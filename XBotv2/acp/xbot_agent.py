"""ACP Agent implementation backed by the XBot session runtime."""

from __future__ import annotations

import asyncio
import re
import shlex
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
from XBotv2.acp.events import ACPEventMapper, replay_history
from XBotv2.core.paths import RuntimePaths
from XBotv2.config.loader import load_provider_names, load_runtime_config
from XBotv2.agentloop.operations import (
    OperationError,
    fork_session as fork_runtime_session,
    select_agent,
    select_provider,
)
from XBotv2.persistence.store import CoreStateStore
from XBotv2.protocol.commands import execute_command, list_commands
from XBotv2.protocol.session_manager import (
    SessionManager,
    SessionNotFound,
    ThreadNotActive,
)

_MCP_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


class XBotACPAgent:
    """Expose XBot as a stable ACP v1 Agent."""

    def __init__(
        self,
        *,
        paths: RuntimePaths,
        provider_name: str,
        no_plugins: bool = False,
        selected_agent: str | None = None,
        llm_override: Any | None = None,
    ) -> None:
        self.paths = paths
        self.provider_name = provider_name
        self.no_plugins = no_plugins
        self.selected_agent = selected_agent
        self.llm_override = llm_override
        self.manager = SessionManager(paths)
        self.connection: Any | None = None
        self.client_capabilities: ClientCapabilities | None = None
        self._commands_announced: set[str] = set()
        self._event_tasks: dict[str, tuple[Any, asyncio.Task[None]]] = {}

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
        runtime = await self.manager.open_session(
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
            llm_override=self.llm_override,
        )
        await self._prepare_runtime(runtime)
        return NewSessionResponse(
            session_id=runtime.session_id,
            config_options=self._config_options(runtime),
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
        runtime = await self._open_existing(session_id, cwd, mcp_servers)
        return ResumeSessionResponse(
            config_options=self._config_options(runtime)
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
        runtime = await self._open_existing(session_id, cwd, mcp_servers)
        await self._replay_history(runtime)
        return LoadSessionResponse(
            config_options=self._config_options(runtime)
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
        root = self.paths.sessions_dir
        for path in sorted(root.iterdir(), reverse=True) if root.is_dir() else []:
            if not path.is_dir():
                continue
            try:
                metadata = _session_metadata(self.paths, path.name)
            except RequestError:
                continue
            workspace = str(metadata.get("workspace_root") or "")
            if not workspace or (
                cwd and Path(workspace).resolve() != Path(cwd).resolve()
            ):
                continue
            sessions.append(SessionInfo(
                session_id=path.name,
                cwd=workspace,
                title=str(metadata.get("title") or path.name),
            ))
        return ListSessionsResponse(sessions=sessions)

    async def close_session(
        self, session_id: str, **_: Any
    ) -> CloseSessionResponse:
        await self.manager.close_session(session_id)
        task_entry = self._event_tasks.pop(session_id, None)
        if task_entry is not None:
            await asyncio.gather(task_entry[1], return_exceptions=True)
        self._commands_announced.discard(session_id)
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
        metadata = _session_metadata(self.paths, session_id)
        workspace = _workspace(cwd)
        stored_workspace = str(metadata.get("workspace_root") or "")
        if stored_workspace and Path(stored_workspace).resolve() != Path(workspace):
            raise RequestError.invalid_params({
                "sessionId": session_id,
                "cwd": cwd,
                "expectedCwd": stored_workspace,
            })

        active = await self.manager.active_threads()
        contexts = [
            runtime
            for (active_session_id, _), runtime in active.items()
            if active_session_id == session_id
        ]
        try:
            forked_id = await fork_runtime_session(
                self.paths,
                session_id,
                *contexts,
            )
        except OperationError as exc:
            raise RequestError.invalid_params({
                "sessionId": session_id,
                "reason": str(exc),
            }) from exc

        runtime = await self._open_existing(forked_id, workspace, mcp_servers)
        return ForkSessionResponse(
            session_id=forked_id,
            config_options=self._config_options(runtime),
        )

    async def prompt(
        self,
        session_id: str,
        prompt: list[Any],
        **_: Any,
    ) -> PromptResponse:
        runtime = await self._runtime(session_id)
        content, images = _prompt_content(
            prompt,
            getattr(runtime.engine, "state_store", None),
        )
        command = _slash_command(runtime, content)
        if command is not None:
            await self._run_command(runtime, *command)
            return PromptResponse(stop_reason="end_turn")

        if session_id not in self._commands_announced:
            await self._announce_commands(runtime)
            self._commands_announced.add(session_id)

        mapper = ACPEventMapper(context_size=runtime.engine.context_window)
        async for event in runtime.stream_message(
            content,
            f"acp:{session_id}",
            images=images,
        ):
            for update in mapper.updates(event):
                await self._update(session_id, update)
        if mapper.error is not None:
            raise RequestError.internal_error(mapper.error)
        return PromptResponse(
            stop_reason=mapper.stop_reason,
            usage=_usage(mapper.usage),
        )

    async def cancel(self, session_id: str, **_: Any) -> None:
        runtime = await self._runtime(session_id)
        runtime.request_interrupt()

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
        runtime = await self._runtime(session_id)
        try:
            if config_id == "agent":
                await select_agent(runtime, value)
            elif config_id == "provider":
                await select_provider(runtime, value)
            else:
                raise RequestError.invalid_params({"configId": config_id})
        except OperationError as exc:
            raise RequestError.invalid_params({
                "configId": config_id,
                "value": value,
                "reason": str(exc),
            }) from exc
        options = self._config_options(runtime)
        await self._update(
            session_id,
            ConfigOptionUpdate(
                session_update="config_option_update",
                config_options=options,
            ),
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
        await self.manager.close_all()
        await asyncio.gather(
            *(entry[1] for entry in self._event_tasks.values()),
            return_exceptions=True,
        )
        self._event_tasks.clear()

    async def _open_existing(
        self,
        session_id: str,
        cwd: str,
        mcp_servers: list[Any] | None = None,
    ) -> Any:
        metadata = _session_metadata(self.paths, session_id)
        stored_workspace = str(metadata.get("workspace_root") or "")
        workspace = _workspace(cwd)
        if stored_workspace and Path(stored_workspace).resolve() != Path(workspace):
            raise RequestError.invalid_params({
                "sessionId": session_id,
                "cwd": cwd,
                "expectedCwd": stored_workspace,
            })
        try:
            runtime = await self.manager.open_session(
                session_id=session_id,
                thread_id="agent",
                provider_name=self.provider_name,
                workspace_root=workspace,
                mode="resume",
                no_plugins=self.no_plugins,
                plugin_configs=self._mcp_plugin_config(
                    workspace, session_id, mcp_servers
                ),
                llm_override=self.llm_override,
            )
        except SessionNotFound as exc:
            raise RequestError.resource_not_found(session_id) from exc
        await self._prepare_runtime(runtime)
        return runtime

    async def _prepare_runtime(self, runtime: Any) -> None:
        # ACP owns interaction requests on its connection. Disabling the SSE
        # interaction bridge leaves Engine's public client sink in control.
        runtime.interactive = False
        runtime.engine.set_client_event_sink(
            lambda event, **kwargs: self._handle_interaction(
                runtime.session_id, event, **kwargs
            )
        )
        existing = self._event_tasks.get(runtime.session_id)
        if existing is not None:
            if existing[0] is runtime and not existing[1].done():
                return
            existing[1].cancel()
            await asyncio.gather(existing[1], return_exceptions=True)
        events = runtime.attach_event_stream()
        task = asyncio.create_task(
            self._forward_session_events(runtime, events),
            name=f"xbot-acp-events-{runtime.session_id}",
        )
        self._event_tasks[runtime.session_id] = (runtime, task)

    async def _runtime(self, session_id: str) -> Any:
        try:
            return await self.manager.get(session_id, "agent")
        except (SessionNotFound, ThreadNotActive) as exc:
            raise RequestError.resource_not_found(session_id) from exc

    async def _forward_session_events(
        self,
        runtime: Any,
        events: asyncio.Queue[dict[str, Any] | None],
    ) -> None:
        mapper = ACPEventMapper(context_size=runtime.engine.context_window)
        try:
            while True:
                event = await events.get()
                if event is None:
                    return
                for update in mapper.updates(event):
                    await self._update(runtime.session_id, update)
        finally:
            runtime.detach_event_stream(events)

    async def _update(self, session_id: str, update: Any) -> None:
        if self.connection is None:
            raise RequestError.internal_error({"reason": "ACP client disconnected"})
        await self.connection.session_update(session_id=session_id, update=update)

    async def _announce_commands(self, runtime: Any) -> None:
        loader = runtime.engine.plugin_loader
        commands = list_commands(extra=loader.commands if loader is not None else ())
        if not commands:
            return
        await self._update(
            runtime.session_id,
            AvailableCommandsUpdate(
                session_update="available_commands_update",
                available_commands=[
                    AvailableCommand(
                        name=item["name"],
                        description=item["description"],
                    )
                    for item in commands
                ],
            ),
        )

    def _config_options(self, runtime: Any) -> list[SessionConfigOptionSelect]:
        options: list[SessionConfigOptionSelect] = []
        registry = getattr(runtime.engine, "agent_registry", None)
        definitions = registry.definitions() if registry is not None else ()
        agents = [
            definition
            for definition in definitions
            if definition.mode != "subagent"
        ]
        if agents:
            active = str(
                runtime.engine.state_store.read_thread_metadata().get("agent")
                or agents[0].name
            )
            options.append(SessionConfigOptionSelect(
                id="agent",
                name="Agent",
                category="_agent",
                type="select",
                current_value=active,
                options=[
                    SessionConfigSelectOption(
                        value=definition.name,
                        name=definition.name,
                        description=definition.description or None,
                    )
                    for definition in agents
                ],
            ))

        _default, provider_names = load_provider_names(self.paths)
        if provider_names:
            options.append(SessionConfigOptionSelect(
                id="provider",
                name="Provider / model",
                category="model",
                type="select",
                current_value=runtime.provider_name,
                options=[
                    SessionConfigSelectOption(value=name, name=name)
                    for name in provider_names
                ],
            ))
        return options

    async def _run_command(
        self, runtime: Any, name: str, raw_args: str
    ) -> None:
        result = await execute_command(
            runtime,
            name,
            shlex.split(raw_args),
            kind="server",
            raw_args=raw_args,
        )
        data = result.get("data") or {}
        await self._update(
            runtime.session_id,
            update_agent_message_text(str(data.get("message") or "")),
        )

    async def _replay_history(self, runtime: Any) -> None:
        for update in replay_history(runtime.engine.messages):
            await self._update(runtime.session_id, update)

    async def _handle_interaction(
        self,
        session_id: str,
        event: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
        tool_call_id: str = "",
    ) -> dict[str, Any]:
        del timeout_seconds
        data = event.get("data") or {}
        request_id = str(data.get("request_id") or "")
        if self.connection is None:
            return {
                "request_id": request_id,
                "status": "disconnected",
                "reason": "ACP client disconnected",
            }
        if event.get("type") == "permission_request":
            call = data.get("tool_call") or {}
            call_id = str(call.get("id") or tool_call_id or request_id)
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
            tool_call_id=tool_call_id or None,
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
    ) -> dict[str, dict[str, Any]] | None:
        if not mcp_servers:
            return None
        if self.no_plugins:
            raise RequestError.invalid_params({
                "mcpServers": "plugins are disabled"
            })
        # Plugin enablement is decided by the plugin tree (xcore.yaml /
        # plugins.yaml); requested servers are injected directly.
        servers: dict[str, Any] = {}
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
        return {"mcp": {"servers": servers}}


def _workspace(cwd: str) -> str:
    path = Path(cwd).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise RequestError.invalid_params({"cwd": cwd})
    return str(path.resolve())


def _session_metadata(paths: RuntimePaths, session_id: str) -> dict[str, Any]:
    try:
        session = paths.session(session_id)
    except ValueError as exc:
        raise RequestError.invalid_params({"sessionId": session_id}) from exc
    if not session.has_thread("agent"):
        raise RequestError.resource_not_found(session_id)
    store = CoreStateStore(
        session,
        thread_id="agent",
        workspace_root="",
        provider="",
    )
    return store.read_thread_metadata()


def _prompt_content(blocks: list[Any], state_store: Any) -> tuple[str, list[Any]]:
    parts: list[str] = []
    images = []
    for block in blocks:
        block_type = getattr(block, "type", "")
        if block_type == "text":
            parts.append(str(block.text))
        elif block_type == "image":
            if state_store is None:
                raise RequestError.invalid_params({
                    "prompt": "image storage is unavailable",
                })
            try:
                images.append(state_store.store_image(
                    str(block.data),
                    str(block.mime_type),
                ))
            except ValueError as exc:
                raise RequestError.invalid_params({
                    "prompt": str(exc),
                }) from exc
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


def _slash_command(runtime: Any, content: str) -> tuple[str, str] | None:
    if not content.startswith("/") or "\n" in content:
        return None
    raw = content[1:]
    name, _, args = raw.partition(" ")
    loader = runtime.engine.plugin_loader
    command = loader.get_command(name) if loader is not None else None
    if command is None or command.kind != "server":
        return None
    return name, args


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
