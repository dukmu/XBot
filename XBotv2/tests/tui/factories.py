"""Shared builders for the TUI test suite.

One scripted backend lives here and is used by every transport test through the
``backend`` fixture. The old suite grew forty private session doubles, each
modelling a different subset of behaviour, which is why none of them could
falsify a real defect. This one models what the transport actually depends on:
sequenced frames, resubscription cursors, a thread listing whose turn status is
authoritative, and failures that are raised rather than returned.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable

from XBotv2.commands import (
    CommandDescription,
    CommandExecution,
    CommandListResponse,
    CommandResponse,
)
from XBotv2.core.domain import (
    AgentExecutionLimits,
    GenerationSettings,
    ModelRoute,
    ReasoningGenerationMode,
    ResolvedModelSelection,
    ResolvedRuntimeSelection,
    SessionScope,
    StandardGenerationMode,
    UsageSnapshot,
)
from XBotv2.core.history import HistoryPage
from XBotv2.core.metadata import ThreadMetadata
from XBotv2.core.domain import CompletedStop, ModelTiming, ToolTiming
from XBotv2.core.tools import ToolCallRef, ToolSucceeded, text_output
from XBotv2.interactions.protocol import InteractionResponse
from XBotv2.protocol import ServerEvent, server_event
from XBotv2.session.contracts import OpenedThread, SessionKey, ThreadSummary
from XBotv2.session.records import AssistantRecord, HumanInputRecord, ToolRecord
from XBotv2.session.protocol import OpenSessionResponse


SESSION = "s1"
THREAD = "agent"

# A real one-pixel PNG: tests that attach an image write these bytes to disk.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AL+2gAAAABJRU5ErkJggg=="
)


def snapshot(**overrides: Any) -> OpenSessionResponse:
    session_id = overrides.pop("session_id", SESSION)
    thread_id = overrides.pop("thread_id", THREAD)
    workspace_root = overrides.pop("workspace_root", "/w")
    provider = overrides.pop("provider", "p")
    model_name = overrides.pop("model", "m")
    agent_name = overrides.pop("agent_name", "XBotv2")
    model_mode = overrides.pop("model_mode", "")
    context_window = overrides.pop("context_window", 4096)
    history = overrides.pop("history", ())
    history_cursor = overrides.pop("history_cursor", None)
    runtime_selection = overrides.pop(
        "runtime_selection",
        ResolvedRuntimeSelection(
            agent_name=agent_name,
            prompt="",
            limits=AgentExecutionLimits(),
            enabled_tools=(),
            model=ResolvedModelSelection(
                route=ModelRoute(provider=provider, model=model_name),
                generation=GenerationSettings(
                    mode=(
                        ReasoningGenerationMode(effort=model_mode)
                        if model_mode
                        else StandardGenerationMode()
                    ),
                    max_output_tokens=context_window,
                ),
                context_window=context_window,
            ),
        ),
    )
    opened = OpenedThread(
        key=SessionKey(session_id=session_id, thread_id=thread_id),
        metadata=ThreadMetadata(
            runtime_selection=runtime_selection,
            workspace_root=workspace_root,
        ),
        history=HistoryPage(items=tuple(history), older_cursor=history_cursor),
        usage=overrides.pop("usage", UsageSnapshot()),
        status_slots=overrides.pop("status_slots", {}),
        event_cursor=overrides.pop("event_cursor", 0),
        pending_inputs=overrides.pop("pending_inputs", ()),
        pending_interactions=overrides.pop("pending_interactions", ()),
        **overrides,
    )
    return OpenSessionResponse(data=opened)


def thread(**overrides: Any) -> ThreadSummary:
    base: dict[str, Any] = {
        "session_id": SESSION,
        "thread_id": THREAD,
        "status": "active",
        "kind": "main",
        "turn_status": "idle",
    }
    return ThreadSummary(**{**base, **overrides})


def human_record(message_id: str, content: str) -> HumanInputRecord:
    return HumanInputRecord(id=message_id, content=content)


def assistant_record(message_id: str, content: str, *, reasoning: str = "") -> dict[str, Any]:
    return AssistantRecord(
        id=message_id,
        content=content,
        reasoning=reasoning,
        timing=ModelTiming(total_ms=0),
        stop=CompletedStop(),
    ).model_dump(mode="json")


def tool_record(call_id: str, name: str, content: str) -> dict[str, Any]:
    return ToolRecord(
        id=f"tool-{call_id}",
        call=ToolCallRef(id=call_id, name=name),
        outcome=ToolSucceeded(output=text_output(content)),
        timing=ToolTiming(duration_ms=0),
    ).model_dump(mode="json")


def history_page(*items: HumanInputRecord, older_cursor: str | None = None) -> HistoryPage:
    return HistoryPage(items=items, older_cursor=older_cursor)


def command(name: str = "status", **overrides: Any) -> CommandDescription:
    """One entry of the catalogue a server publishes."""
    base: dict[str, Any] = {
        "name": name,
        "slash": f"/{name}",
        "kind": "server",
        "description": f"the {name} command",
        "usage": f"/{name}",
        "exclusive": True,
    }
    return CommandDescription(**{**base, **overrides})


def execution(**overrides: Any) -> CommandExecution:
    base: dict[str, Any] = {
        "command": "status",
        "status": "ok",
        "message": "session s1 · thread agent · idle",
    }
    return CommandExecution(**{**base, **overrides})


def model(name: str = "m1", **overrides: Any) -> Any:
    from XBotv2.llm.contracts import ModelDescription

    base: dict[str, Any] = {
        "model": name,
        "max_context_tokens": 4096,
        "effort": ("low", "high"),
    }
    return ModelDescription(**{**base, **overrides})


def catalog(**overrides: Any) -> Any:
    """A provider catalogue: one provider with two models, one the default."""
    from XBotv2.llm.contracts import ProviderCatalog, ProviderDescription

    base: dict[str, Any] = {
        "default": "p1",
        "providers": (
            ProviderDescription(
                name="p1",
                provider="openai",
                default_model="m1",
                models=(model("m1"), model("m2", effort=("low",))),
            ),
            ProviderDescription(
                name="p2",
                provider="anthropic",
                default_model="m3",
                models=(model("m3"),),
            ),
        ),
    }
    return ProviderCatalog(**{**base, **overrides})


def agent_list(**overrides: Any) -> Any:
    from XBotv2.agents.protocol import AgentInfo, AgentListResponse
    from XBotv2.agents.contracts import AgentModelPolicy, AgentToolPolicy
    from XBotv2.core.domain import AgentExecutionLimits

    def info(name: str, description: str) -> AgentInfo:
        return AgentInfo(
            name=name,
            description=description,
            mode="primary",
            model_policy=AgentModelPolicy(),
            limits=AgentExecutionLimits(),
            tool_policy=AgentToolPolicy(),
        )

    base: dict[str, Any] = {
        "active": "default",
        "agents": [
            info("default", "the default agent"),
            info("reviewer", "reviews diffs"),
        ],
    }
    return AgentListResponse(**{**base, **overrides})


def selection_response(**overrides: Any) -> Any:
    from XBotv2.agents.protocol import AgentSelectionResponse

    base: dict[str, Any] = {"session_id": SESSION, "thread_id": THREAD, "agent": "default"}
    return AgentSelectionResponse(**{**base, **overrides})


def frame(
    frame_type: str,
    data: dict[str, Any] | None = None,
    *,
    sequence: int = 1,
    session_id: str = SESSION,
    thread_id: str = THREAD,
) -> ServerEvent:
    return server_event(
        kind=frame_type,
        payload=data or {},
        sequence=sequence,
        session_id=session_id,
        thread_id=thread_id,
        scope=SessionScope(),
    )


@dataclass
class StreamScript:
    """One resubscription: what the server does the next time it is asked.

    ``frames`` are yielded in order; ``fail_with`` is raised instead when set.
    ``after`` records the cursor the client asked to resume from, so a test can
    assert that recovery resumed where it claimed.
    """

    frames: tuple[ServerEvent, ...] = ()
    fail_with: BaseException | None = None
    end_after_frames: bool = True
    after: int | None = None
    session_id: str = ""
    """Recorded: the session this subscription was opened for."""
    thread_id: str = ""
    hold: bool = False
    """Keep the subscription open after the frames, as a real server does."""


@dataclass
class ScriptedBackend:
    """A scripted server: sequenced frames, authoritative thread reads, failures.

    Deliberately blunt -- it replays exactly the scripts it was given, in order,
    and records what the client asked for.
    """

    session: OpenSessionResponse = field(default_factory=snapshot)
    threads: tuple[ThreadSummary, ...] = field(default_factory=lambda: (thread(),))
    streams: list[StreamScript] = field(default_factory=list)
    session_catalog: list[Any] = field(default_factory=list)
    sent: list[dict[str, Any]] = field(default_factory=list)
    opened: list[dict[str, Any]] = field(default_factory=list)
    interrupts: int = 0
    interaction_responses: list[dict[str, Any]] = field(default_factory=list)
    interrupt_cancelled: bool = True
    interrupt_error: BaseException | None = None
    send_error: BaseException | None = None
    thread_error: BaseException | None = None
    open_error: BaseException | None = None
    closed: bool = False
    stream_opens: int = 0
    # Every subscription this backend handed out, scripted or implicit, so a
    # test can assert where a resubscription actually pointed.
    subscriptions: list[StreamScript] = field(default_factory=list)

    # What ``hello`` answers with. A real server answers with the session it was
    # asked about, and with nothing when it was asked for no session in
    # particular -- that is the ``xbot tui`` default (no ``--session``).
    hello_session_id: str = SESSION
    hello_thread_id: str = THREAD
    #: The session ids the client asked a thread listing for, in order.
    read_threads: list[str] = field(default_factory=list)

    # --- lifecycle ----------------------------------------------------
    async def hello(self, **kwargs: Any) -> Any:
        return type(
            "Hello",
            (),
            {"session_id": self.hello_session_id, "thread_id": self.hello_thread_id},
        )()

    async def open_session(self, **kwargs: Any) -> OpenSessionResponse:
        self.opened.append(kwargs)
        if self.open_error is not None:
            raise self.open_error
        return self.session

    async def close(self) -> None:
        self.closed = True

    async def list_sessions(self) -> Any:
        return type("Sessions", (), {"sessions": list(self.session_catalog), "event_cursor": 0})()

    # --- provider / agent / effort selection --------------------------
    providers: Any = None
    providers_error: BaseException | None = None
    agents: Any = None
    agents_error: BaseException | None = None
    selections: list[dict[str, Any]] = field(default_factory=list)
    selection_error: BaseException | None = None

    async def list_providers(self) -> Any:
        if self.providers_error is not None:
            raise self.providers_error
        return self.providers if self.providers is not None else catalog()

    async def list_agents(self, session_id: str, thread_id: str) -> Any:
        if self.agents_error is not None:
            raise self.agents_error
        return self.agents if self.agents is not None else agent_list()

    def _record_selection(self, **fields: Any) -> None:
        self.selections.append(fields)
        if self.selection_error is not None:
            raise self.selection_error

    async def select_provider(
        self, session_id: str, thread_id: str, name: str, model: str | None = None
    ) -> Any:
        self._record_selection(kind="provider", name=name, model=model)
        return selection_response()

    async def select_effort(self, session_id: str, thread_id: str, effort: str) -> Any:
        self._record_selection(kind="effort", effort=effort)
        return selection_response()

    async def select_agent(self, session_id: str, thread_id: str, name: str) -> Any:
        self._record_selection(kind="agent", name=name)
        return selection_response()

    # --- the command plane --------------------------------------------
    commands: tuple[CommandDescription, ...] = ()
    commands_error: BaseException | None = None
    command_result: CommandExecution | None = None
    command_error: BaseException | None = None
    #: ``(session_id, thread_id)`` reads, in order.
    command_reads: list[tuple[str, str]] = field(default_factory=list)
    command_calls: list[dict[str, Any]] = field(default_factory=list)

    async def list_commands(self, session_id: str, thread_id: str) -> CommandListResponse:
        self.command_reads.append((session_id, thread_id))
        if self.commands_error is not None:
            raise self.commands_error
        return CommandListResponse(commands=list(self.commands))

    async def run_command(
        self, session_id: str, thread_id: str, *, raw: str
    ) -> CommandResponse:
        self.command_calls.append(
            {"session_id": session_id, "thread_id": thread_id, "raw": raw}
        )
        if self.command_error is not None:
            raise self.command_error
        result = self.command_result or execution()
        return CommandResponse(data=result)

    # --- older history pages ------------------------------------------
    #: Pages handed out in order, one per read; the last one repeats when a test
    #: reads more times than it scripted.
    pages: list[Any] = field(default_factory=list)
    page_error: BaseException | None = None
    #: ``{session_id, thread_id, cursor, limit}`` for every read, in order.
    page_reads: list[dict[str, Any]] = field(default_factory=list)

    async def list_messages(
        self,
        session_id: str,
        thread_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> Any:
        self.page_reads.append({
            "session_id": session_id,
            "thread_id": thread_id,
            "cursor": cursor,
            "limit": limit,
        })
        if self.page_error is not None:
            raise self.page_error
        if len(self.pages) > 1:
            return self.pages.pop(0)
        if self.pages:
            return self.pages[0]
        return HistoryPage(items=(), older_cursor=None)

    # --- reads --------------------------------------------------------
    async def list_threads(self, session_id: str) -> Any:
        self.read_threads.append(session_id)
        if self.thread_error is not None:
            raise self.thread_error
        return type("Threads", (), {"session_id": session_id, "threads": list(self.threads)})()

    # --- writes -------------------------------------------------------
    async def send_message(
        self,
        session_id: str,
        thread_id: str,
        content: str,
        **kwargs: Any,
    ) -> None:
        self.sent.append(
            {
                "session_id": session_id,
                "thread_id": thread_id,
                "content": content,
                **kwargs,
            }
        )
        if self.send_error is not None:
            raise self.send_error

    async def interrupt(self, session_id: str, thread_id: str) -> Any:
        self.interrupts += 1
        if self.interrupt_error is not None:
            raise self.interrupt_error
        return type(
            "Interrupt",
            (),
            {"session_id": session_id, "thread_id": thread_id,
             "status": "interrupting", "cancelled": self.interrupt_cancelled},
        )()

    async def respond_permission(
        self,
        session_id: str,
        thread_id: str,
        *,
        request_id: str,
        decision: str,
        scope: str = "once",
    ) -> InteractionResponse:
        self.interaction_responses.append({
            "kind": "permission",
            "session_id": session_id,
            "thread_id": thread_id,
            "request_id": request_id,
            "decision": decision,
            "scope": scope,
        })
        return InteractionResponse(request_id=request_id)

    async def respond_user_input(
        self,
        session_id: str,
        thread_id: str,
        *,
        request_id: str,
        answer: str,
    ) -> InteractionResponse:
        self.interaction_responses.append({
            "kind": "user_input",
            "session_id": session_id,
            "thread_id": thread_id,
            "request_id": request_id,
            "answer": answer,
        })
        return InteractionResponse(request_id=request_id)

    # --- stream -------------------------------------------------------
    def stream_events(
        self,
        session_id: str,
        thread_id: str,
        *,
        after: int | None = None,
    ) -> AsyncIterator[ServerEvent]:
        script = self.script_for(after, session_id=session_id, thread_id=thread_id)
        return self._replay(script)

    def script_for(
        self,
        after: int | None,
        *,
        session_id: str = "",
        thread_id: str = "",
    ) -> StreamScript:
        index = self.stream_opens
        self.stream_opens += 1
        if index < len(self.streams):
            script = self.streams[index]
        else:
            # A real event subscription stays open; an unscripted one must too,
            # or every test would be measuring a server that hung up.
            script = StreamScript(hold=True)
        script.after = after
        script.session_id = session_id
        script.thread_id = thread_id
        self.subscriptions.append(script)
        return script

    async def _replay(self, script: StreamScript) -> AsyncIterator[ServerEvent]:
        if script.fail_with is not None:
            raise script.fail_with
        for event in script.frames:
            yield event
        if script.hold:
            await asyncio.Event().wait()


def stream(*frames: ServerEvent, **kwargs: Any) -> StreamScript:
    return StreamScript(frames=tuple(frames), **kwargs)


def streams(*scripts: StreamScript) -> list[StreamScript]:
    return list(scripts)


def frames(*pairs: tuple[str, dict[str, Any]]) -> tuple[ServerEvent, ...]:
    """Sequenced frames from ``(type, data)`` pairs, numbered from 1."""
    return tuple(
        frame(frame_type, data, sequence=index)
        for index, (frame_type, data) in enumerate(pairs, start=1)
    )


def iter_frames(events: Iterable[ServerEvent]) -> list[ServerEvent]:
    return list(events)


@dataclass
class RecordingView:
    """The one view double: records what the controller asks it to render.

    It is a port with a handful of methods, not a second implementation of the
    client, so the controller can be checked without a terminal while the real
    adapter is checked against a live Textual app elsewhere.
    """

    transcripts: list[Any] = field(default_factory=list)
    statuses: list[Any] = field(default_factory=list)
    job_lists: list[Any] = field(default_factory=list)
    queues: list[Any] = field(default_factory=list)
    composers: list[Any] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    tail_calls: int = 0
    #: Whether the reader is following the tail; tests flip it to stand in for a
    #: reader who scrolled back.
    at_end: bool = True
    #: Whether moving the window is possible; False means the window is already
    #: at the oldest entry the client holds.
    can_move_older: bool = True

    @property
    def reader_at_end(self) -> bool:
        return self.at_end

    async def render_transcript(self, state: Any) -> bool:
        self.transcripts.append(state)
        return True

    def render_status(self, model: Any) -> None:
        self.statuses.append(model)

    def render_jobs(self, jobs: Any) -> None:
        self.job_lists.append(tuple(jobs))

    def render_queue(self, items: Any) -> None:
        self.queues.append(tuple(items))

    def render_composer(self, model: Any) -> None:
        self.composers.append(model)

    async def page_older(self, state: Any) -> bool:
        self.pages.append("older")
        return self.can_move_older

    async def page_newer(self, state: Any) -> bool:
        self.pages.append("newer")
        return True

    async def go_to_tail(self, state: Any) -> None:
        self.pages.append("tail")

    @property
    def renders(self) -> int:
        return len(self.transcripts)

    @property
    def last_status(self) -> Any:
        return self.statuses[-1] if self.statuses else None

    @property
    def last_composer(self) -> Any:
        return self.composers[-1] if self.composers else None
