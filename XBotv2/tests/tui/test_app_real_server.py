"""The whole stack against a real server, over a real HTTP contract.

Every other test in this suite scripts the server. This one does not: it builds
the actual FastAPI application with a mock model, serves it on an ephemeral
loopback port, and drives the real TUI through it. It is the only test that can
show the client and the server agree about the event sequence, the cursor, the
delivery semantics, and the turn lifecycle.

``httpx.ASGITransport`` cannot be used here: it buffers a response body before
returning it, so an SSE subscription never completes.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import subprocess
import sys
import uuid
from functools import partial
from pathlib import Path
from typing import AsyncIterator, Callable

import pytest
import pytest_asyncio
import uvicorn
import yaml
from rich.cells import cell_len
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from textual.widgets import Static

from XBotv2.client import XBotClient
from XBotv2.core.domain import ProviderError
from XBotv2.core.history import SurfaceReplaced
from XBotv2.core.messages import CompactionSummaryMessage
from XBotv2.core.stream import ModelCompleted, ModelFailed
from XBotv2.core.paths import RuntimePaths
from XBotv2.interactions.protocol import UserInputRequest
from XBotv2.llm.mock import MockLLM
from XBotv2.permissions.contracts import PermissionRequest
from XBotv2.application.app import create_agent_application
from XBotv2.application.server import start_server_application
from XBotv2.session.records import AssistantRecord, HumanInputRecord
from XBotv2.tests.tui.factories import PNG_BYTES, tui_app
from XBotv2.tui.app import TuiApp
from XBotv2.tui.transport import TransportConfig
from XBotv2.tui.view.composer import Composer
from XBotv2.tui.view.jobs import JobPanel
from XBotv2.tui.view.status_bar import StatusBar
from XBotv2.tui.view.transcript import TranscriptScroll

SESSION_ID = "tui-e2e"
THREAD_ID = "agent"
REPLY = "hello from the real server"
CAPTION = "A title from the caption plugin"


@pytest_asyncio.fixture
async def base_url(
    tmp_path: Path, request: pytest.FixtureRequest
) -> AsyncIterator[str]:
    """A real XBotv2 HTTP server on an ephemeral port, with a mocked model.

    ``httpx.ASGITransport`` buffers a whole response before returning it, so it
    cannot carry a Server-Sent Events stream at all: the subscription never
    completes. Driving a real uvicorn server is therefore the only way to test the
    client's event reader and everything built on it.
    """
    data_dir = tmp_path / "data"
    scenario = getattr(request, "param", "core")
    no_plugins = scenario in {
        "core", "permission", "permission_deny", "interaction", "thinking_stream",
        "thinking_activity", "queue_stream", "minimax_thinking",
    }
    upstream_http: uvicorn.Server | None = None
    upstream_serving: asyncio.Task | None = None
    provider_request_path = tmp_path / "minimax-provider-request.json"
    provider_base_url = ""
    if scenario == "minimax_thinking":
        upstream = FastAPI()

        async def minimax_stream():
            yield (
                'event: message_start\ndata: {"type":"message_start",'
                '"message":{"id":"message-1","type":"message",'
                '"role":"assistant","content":[],"model":"MiniMax-M3",'
                '"stop_reason":null,"stop_sequence":null,"usage":'
                '{"input_tokens":12,"output_tokens":0}}}\n\n'
            )
            yield (
                'event: content_block_start\ndata: '
                '{"type":"content_block_start","index":0,"content_block":'
                '{"type":"thinking","thinking":null,"signature":null}}\n\n'
            )
            yield (
                'event: content_block_delta\ndata: '
                '{"type":"content_block_delta","index":0,"delta":'
                '{"type":"thinking_delta","thinking":"Checking the request."}}\n\n'
            )
            await asyncio.sleep(0.6)
            yield (
                'event: content_block_stop\ndata: '
                '{"type":"content_block_stop","index":0}\n\n'
            )
            yield (
                'event: content_block_start\ndata: '
                '{"type":"content_block_start","index":1,"content_block":'
                '{"type":"text","text":null}}\n\n'
            )
            yield (
                'event: content_block_delta\ndata: '
                '{"type":"content_block_delta","index":1,"delta":'
                '{"type":"text_delta","text":"MiniMax response."}}\n\n'
            )
            yield (
                'event: content_block_stop\ndata: '
                '{"type":"content_block_stop","index":1}\n\n'
            )
            yield (
                'event: message_delta\ndata: '
                '{"type":"message_delta","delta":{"stop_reason":"end_turn",'
                '"stop_sequence":null},"usage":{"output_tokens":6}}\n\n'
            )
            yield 'event: message_stop\ndata: {"type":"message_stop"}\n\n'

        @upstream.post("/anthropic/v1/messages")
        async def minimax_messages(payload: Request) -> StreamingResponse:
            body = await payload.json()
            provider_options = {
                key: body[key]
                for key in ("thinking", "reasoning_effort")
                if key in body
            }
            provider_request_path.write_text(
                json.dumps(provider_options, ensure_ascii=False), encoding="utf-8"
            )
            return StreamingResponse(
                minimax_stream(), media_type="text/event-stream"
            )

        upstream_config = uvicorn.Config(
            upstream,
            host="127.0.0.1",
            port=0,
            log_level="warning",
            lifespan="off",
        )
        upstream_http = uvicorn.Server(upstream_config)
        upstream_serving = asyncio.create_task(
            upstream_http.serve(), name="minimax-compatible-test-api"
        )
        deadline = asyncio.get_running_loop().time() + 10
        while not upstream_http.started:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("the MiniMax-compatible test API did not start")
            await asyncio.sleep(0.01)
        upstream_port = upstream_http.servers[0].sockets[0].getsockname()[1]
        provider_base_url = f"http://127.0.0.1:{upstream_port}/anthropic"

    (data_dir / "config").mkdir(parents=True)
    provider_name = "default"
    if scenario == "minimax_thinking":
        provider_name = "minimax"
        llm_config = {
            "default_provider": provider_name,
            "providers": {
                provider_name: {
                    "protocol": "anthropic",
                    "base_url": provider_base_url,
                    "api_key": "test",
                    "default_model": "MiniMax-M3",
                    "models": [{
                        "model": "MiniMax-M3",
                        "max_context_tokens": 1_000_000,
                        "max_output_tokens": 1024,
                        "thinking": "adaptive",
                    }],
                }
            },
        }
    else:
        llm_config = {
            "default_provider": "default",
            "providers": {
                "default": {
                    "protocol": "openai",
                    "base_url": "http://test",
                    "api_key": "test",
                    "default_model": "test",
                    "models": [{
                        "model": "test",
                        "max_context_tokens": 4096,
                        "max_output_tokens": 1024,
                    }],
                }
            },
        }
    plugin_entries = [
        {
            "id": "llm",
            "name": "llm",
            "config": llm_config,
        },
        {
            "id": "config",
            "name": "config",
            "config": {
                "user": {
                    "user_id": "test",
                    "user_name": "Tester",
                    "platform": "tui",
                    "session_type": "interactive",
                }
            },
        },
        {"id": "sandbox", "name": "sandbox", "config": {"enabled": False, "resources": []}},
        {
            "id": "permissions",
            "name": "permissions",
            "config": {
                "default_decision": "allow",
                "rules": [
                    {"tool_pattern": "ask_user", "decision": "ask"},
                    {"tool_pattern": "edit", "decision": "ask"},
                ],
            },
        },
    ]
    if not no_plugins:
        disabled_plugins = (
            "goal", "todolist", "skills", "mcp_plugin", "compact",
            "subagents", "browser", "token_manager",
            "workspace_instructions",
        )
        plugin_entries.extend(
            {"id": plugin_id, "disabled": True}
            for plugin_id in disabled_plugins
            if not (
                (scenario == "compact" and plugin_id == "compact")
                or (
                    scenario in {"subagent", "subagent_running"}
                    and plugin_id == "subagents"
                )
            )
        )
    if scenario == "compact":
        plugin_entries.append({
            "id": "compact",
            "name": "compact",
            "config": {"automatic": False, "keep_recent_turns": 4},
        })
    (data_dir / "config" / "plugins.yaml").write_text(
        yaml.safe_dump(plugin_entries, sort_keys=False),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if scenario in {"subagent", "subagent_running"}:
        (workspace / ".agents").mkdir()
        (workspace / ".agents" / "reviewer.md").write_text(
            "---\ndescription: Review a change\nmode: subagent\n---\nReview.",
            encoding="utf-8",
        )
    server = await start_server_application(
        provider_name=provider_name,
        paths=RuntimePaths.from_data_dir(data_dir),
        workspace_root=str(workspace),
        no_plugins=no_plugins,
    )
    answer = {
        "content": REPLY,
        "reasoning": "I am checking the request before answering.",
        "usage_metadata": {
            "input_tokens": 120,
            "output_tokens": 35,
            "cache_read_input_tokens": 30,
        },
    }
    model_responses = [answer] * 6
    model_factory = MockLLM
    if scenario == "caption":
        model_responses.insert(0, {"content": CAPTION})
    elif scenario == "caption_retry":
        model_responses = [answer, {"content": CAPTION}, answer, *([answer] * 3)]

        class FailureOnceMockLLM(MockLLM):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.failed_caption_call = False

            async def _astream_once(self, model_request):
                if not self.failed_caption_call:
                    self.failed_caption_call = True
                    yield ModelFailed(
                        error=ProviderError(
                            code="caption_temporarily_unavailable",
                            message="temporary caption outage",
                            retryable=True,
                            category="transport",
                        )
                    )
                    return
                async for event in super()._astream_once(model_request):
                    yield event

        model_factory = FailureOnceMockLLM
    elif scenario in {"permission", "permission_deny"}:
        file_name = "approval.txt" if scenario == "permission" else "denied.txt"
        file_content = (
            "approved by the user"
            if scenario == "permission"
            else "must never be written"
        )
        model_responses = [
            {
                "tool_calls": [{
                    "id": "permission-edit",
                    "name": "edit",
                    "args": {
                        "path": file_name,
                        "mode": "write",
                        "content": file_content,
                    },
                }],
            },
            answer,
        ]
    elif scenario == "interaction":
        model_responses = [
            {
                "tool_calls": [{
                    "id": "ask-user-call",
                    "name": "ask_user",
                    "args": {
                        "question": "Which deployment window?",
                        "options": [
                            {"label": "Tuesday", "description": "Lower traffic"},
                            {"label": "Thursday", "description": "More staff"},
                        ],
                    },
                }],
            },
            answer,
            *([answer] * 3),
        ]
    elif scenario == "thinking_stream":
        model_responses = [
            {
                **answer,
                "chunks": [
                    {"reasoning": "First, I am checking "},
                    {"reasoning": "the request before answering."},
                    {"content": REPLY},
                ],
                "chunk_delay_ms": 300,
            },
            {"content": REPLY},
        ]
    elif scenario == "thinking_activity":
        model_responses = [
            {
                "content": REPLY,
                "chunks": [{"content": REPLY}],
                "chunk_delay_ms": 2500,
            },
        ]
    elif scenario == "queue_stream":
        model_responses = [
            {
                **answer,
                "content": "first turn complete",
                "chunks": [
                    {"content": "first turn is still working"},
                    {"content": "; now completing"},
                ],
                "chunk_delay_ms": 6000,
            },
            {"content": "queued follow-up complete"},
        ]
    elif scenario == "compact":
        model_responses = [
            {"content": CAPTION},
            *[{'content': f'answer {index}'} for index in range(5)],
            {"content": "summary of the earlier discussion"},
            {"content": "answer after compact resume"},
        ]
    elif scenario in {"subagent", "subagent_running"}:
        class AgentRoutedMockLLM(MockLLM):
            def __init__(self, **kwargs):
                super().__init__(responses=[])
                self.parent_responses = [
                    {"content": "Subagent review"},
                    {"tool_calls": [{
                        "id": "spawn-reviewer",
                        "name": "spawn_subagent",
                        "args": {
                            "agent": "reviewer",
                            "prompt": "Inspect the proposed change.",
                        },
                    }]},
                ]
                if scenario == "subagent":
                    self.parent_responses.extend([
                        {"tool_calls": [{
                            "id": "wait-reviewer",
                            "name": "wait_subagent",
                            "args": {"mode": "all"},
                        }]},
                        {"content": "The delegated review is complete."},
                    ])
                else:
                    self.parent_responses.append({
                        "content": "The delegated review is running in the background."
                    })
                self.child_responses = [
                    {"content": "The reviewer found no blocking issue."},
                ]

            async def _astream_once(self, model_request):
                system_text = "\n".join(
                    part.text
                    for message in model_request.messages
                    for part in message.parts
                    if hasattr(part, "text")
                )
                responses = (
                    self.child_responses
                    if "Review." in system_text
                    else self.parent_responses
                )
                if responses is self.child_responses and scenario == "subagent_running":
                    await asyncio.sleep(30)
                yield ModelCompleted(response=self.to_response(responses.pop(0)))

        model_factory = AgentRoutedMockLLM
    if scenario != "minimax_thinking":
        server.sessions.application_factory = partial(
            create_agent_application,
            model_override=model_factory(
                responses=model_responses,
                # The override bypasses the plugin config, so its capabilities have to
                # be declared here; attaching an image is otherwise rejected.
                input_modalities=["text", "image"],
            ),
        )
    config = uvicorn.Config(
        server.server,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        lifespan="off",
    )
    http = uvicorn.Server(config)
    serving = asyncio.create_task(http.serve(), name="tui-e2e-server")
    try:
        deadline = asyncio.get_running_loop().time() + 10
        while not http.started:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("the test server did not start")
            await asyncio.sleep(0.01)
        port = http.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        http.should_exit = True
        await asyncio.gather(serving, return_exceptions=True)
        await server.stop()
        if upstream_http is not None and upstream_serving is not None:
            upstream_http.should_exit = True
            await asyncio.gather(upstream_serving, return_exceptions=True)


@pytest_asyncio.fixture
async def real_client(base_url: str) -> AsyncIterator[XBotClient]:
    # No environment scrubbing here on purpose: a loopback client must be
    # buildable on a machine whose proxy variables are hostile (see
    # ``tests/core/test_client.py``), and this fixture is where that is proven
    # end to end rather than worked around.
    #
    client = XBotClient(base_url, timeout=10.0)
    try:
        yield client
    finally:
        await client.close()


def tui(client: XBotClient) -> TuiApp:
    return tui_app(
        client,
        config=TransportConfig(
            session_id=SESSION_ID,
            thread_id=THREAD_ID,
            mode="new",
            reconnect_delays=(0.05, 0.1, 0.25),
        ),
        render_interval=0.01,
    )


def status_text(app: TuiApp) -> str:
    content = app.query_one("#status", StatusBar).content
    return str(getattr(content, "plain", "") or "")


def transcript_text(app: TuiApp) -> str:
    scroll = app.query_one("#transcript", TranscriptScroll)
    parts: list[str] = []
    for entry in scroll.children:
        for widget in entry.query("Static"):
            content = getattr(widget, "content", None)
            parts.append(
                str(getattr(content, "plain", "") or getattr(content, "markup", "") or "")
            )
    return "\n".join(parts)


async def wait_for(
    pilot,
    predicate: Callable[[], bool],
    *,
    seconds: float = 8.0,
    description: str = "condition",
) -> None:
    """Pause the app until ``predicate`` holds, or fail with what was on screen."""
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
        await pilot.pause()
    raise AssertionError(f"timed out waiting for {description}")


# --- the handshake --------------------------------------------------------


def _tmux(*args: str) -> str:
    result = subprocess.run(
        ["tmux", *args],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode:
        raise AssertionError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def _resize_tmux_window(session_name: str, width: int, height: int) -> None:
    """Set and verify detached-window dimensions; new-session -x/-y may be ignored."""
    _tmux(
        "resize-window",
        "-t",
        session_name,
        "-x",
        str(width),
        "-y",
        str(height),
    )
    actual = _tmux(
        "display-message",
        "-p",
        "-t",
        session_name,
        "#{pane_width}x#{pane_height}",
    ).strip()
    expected = f"{width}x{height}"
    if actual != expected:
        raise AssertionError(
            f"tmux pane {session_name!r} is {actual}; expected {expected}"
        )


def _capture_tmux_screen(session_name: str) -> str:
    return _tmux("capture-pane", "-p", "-J", "-t", session_name)


async def _wait_for_tmux_screen(
    session_name: str,
    predicate: Callable[[str], bool],
    *,
    description: str,
    seconds: float = 10.0,
) -> str:
    deadline = asyncio.get_running_loop().time() + seconds
    screen = ""
    while asyncio.get_running_loop().time() < deadline:
        try:
            screen = _capture_tmux_screen(session_name)
        except AssertionError:
            screen = ""
        if predicate(screen):
            return screen
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {description}; screen:\n{screen}")


async def _stop_tmux_tui(session_name: str) -> None:
    """Exit one real TUI and prove its PTY process stopped."""
    _tmux("send-keys", "-t", session_name, "C-c")
    deadline = asyncio.get_running_loop().time() + 5.0
    screen = ""
    while asyncio.get_running_loop().time() < deadline:
        try:
            screen = _capture_tmux_screen(session_name)
            pane_dead = _tmux(
                "display-message", "-p", "-t", session_name, "#{pane_dead}"
            ).strip()
        except AssertionError:
            return
        if pane_dead == "1":
            return
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"Ctrl-C did not exit real CLI TUI {session_name!r}; screen:\n{screen}"
    )


@pytest.mark.parametrize("base_url", ["interaction"], indirect=True)
async def test_real_cli_tui_pty_completes_permission_and_user_input_round_trip(
    real_client: XBotClient,
    base_url: str,
    tmp_path: Path,
) -> None:
    """Drive the installed CLI in tmux, not Textual's in-process pilot."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for the real TTY interaction smoke")

    session_name = f"xbot-tui-{uuid.uuid4().hex[:10]}"
    repo_root = Path(__file__).resolve().parents[3]
    xbot = Path(sys.executable).with_name("xbot")
    command = shlex.join([
        str(xbot),
        "tui",
        "--server",
        base_url,
        "--no-plugins",
    ])
    captures = tmp_path / "pty-captures"
    captures.mkdir()
    try:
        _tmux(
            "new-session",
            "-d",
            "-s",
            session_name,
            "-x",
            "120",
            "-y",
            "40",
            "-c",
            str(repo_root),
            command,
        )
        _resize_tmux_window(session_name, 120, 40)
        ready = await _wait_for_tmux_screen(
            session_name,
            lambda screen: "Ready" in screen,
            description="the real CLI TUI to connect over PTY",
        )
        (captures / "ready.txt").write_text(ready, encoding="utf-8")

        _tmux("send-keys", "-t", session_name, "-l", "ask me a question")
        _tmux("send-keys", "-t", session_name, "Enter")
        permission_screen = await _wait_for_tmux_screen(
            session_name,
            lambda screen: "ask_user" in screen
            and "ID:" in screen
            and "Allow once" in screen
            and "Allow for this session" in screen
            and "Deny" in screen,
            description="the permission request rendered in the real TTY",
        )
        assert '"options": [' not in permission_screen
        assert any(
            line.lstrip().startswith("● ask_user")
            for line in permission_screen.splitlines()
        ), "the running tool step needs a compact action marker"
        assert "Tool: ask_user" in permission_screen
        assert "Enter confirm · Esc deny" in permission_screen
        (captures / "permission.txt").write_text(
            permission_screen, encoding="utf-8"
        )

        _resize_tmux_window(session_name, 80, 24)
        permission_compact = await _wait_for_tmux_screen(
            session_name,
            lambda screen: (
                "Permission required" in screen
                and "Tool: ask_user" in screen
                and "Allow for this session" in screen
                and "Enter confirm · Esc deny" in screen
                and "╮" in screen
                and "╯" in screen
            ),
            description="the permission card after an 80x24 resize",
        )
        assert all(cell_len(line) <= 80 for line in permission_compact.splitlines())
        (captures / "permission-80x24.txt").write_text(
            permission_compact, encoding="utf-8"
        )
        _resize_tmux_window(session_name, 120, 40)

        _tmux("send-keys", "-t", session_name, "C-e")
        tool_expanded = await _wait_for_tmux_screen(
            session_name,
            lambda screen: "ctrl+e collapses" in screen and '"options": [' in screen,
            description="expanded tool arguments in the transcript",
        )
        assert "Permission required" in tool_expanded
        assert "Tool: ask_user" in tool_expanded
        assert "Allow once" in tool_expanded
        assert "Deny" in tool_expanded
        (captures / "tool-expanded.txt").write_text(
            tool_expanded, encoding="utf-8"
        )
        _tmux("send-keys", "-t", session_name, "C-e")
        permission_screen = await _wait_for_tmux_screen(
            session_name,
            lambda screen: "? Permission required" in screen
            and '"options": [' not in screen,
            description="folded tool arguments hidden again",
        )

        sessions = await real_client.list_sessions()
        assert len(sessions.sessions) == 1
        session_id = sessions.sessions[0].session_id
        opened = await real_client.open_session(
            session_id=session_id,
            thread_id=THREAD_ID,
            mode="resume",
        )
        permission = next(
            item
            for item in opened.data.pending_interactions
            if isinstance(item, PermissionRequest)
        )
        _tmux("send-keys", "-t", session_name, "Enter")
        question_screen = await _wait_for_tmux_screen(
            session_name,
            lambda screen: "Which deployment window?" in screen
            and "Tuesday" in screen
            and "Thursday" in screen
            and "Requested by ask_user" in screen
            and "Enter confirm · Esc dismiss" in screen,
            description="the user-input request after permission approval",
        )
        assert "Requested by ask_user" in question_screen
        assert "Enter confirm · Esc dismiss" in question_screen
        (captures / "question.txt").write_text(question_screen, encoding="utf-8")

        _resize_tmux_window(session_name, 80, 24)
        question_compact = await _wait_for_tmux_screen(
            session_name,
            lambda screen: (
                "Question" in screen
                and "Which deployment window?" in screen
                and "Requested by ask_user" in screen
                and "Thursday" in screen
                and "Enter confirm · Esc dismiss" in screen
                and "╮" in screen
                and "╯" in screen
            ),
            description="the question card after an 80x24 resize",
        )
        assert all(cell_len(line) <= 80 for line in question_compact.splitlines())
        (captures / "question-80x24.txt").write_text(
            question_compact, encoding="utf-8"
        )
        _resize_tmux_window(session_name, 120, 40)

        opened = await real_client.open_session(
            session_id=session_id,
            thread_id=THREAD_ID,
            mode="resume",
        )
        question = next(
            item
            for item in opened.data.pending_interactions
            if isinstance(item, UserInputRequest)
        )
        _tmux("send-keys", "-t", session_name, "Enter")
        answer_screen = await _wait_for_tmux_screen(
            session_name,
            lambda screen: REPLY in screen
            and "I am checking the request" in screen,
            description="the completed answer and Think block",
        )
        (captures / "answer.txt").write_text(answer_screen, encoding="utf-8")

        long_tail = ("second pasted line 二 " + ("long paste word " * 100)).rstrip()
        _tmux(
            "set-buffer",
            "-b",
            "xbot-follow-up",
            f"follow-up question\n{long_tail}",
        )
        _tmux(
            "paste-buffer",
            "-p",
            "-r",
            "-b",
            "xbot-follow-up",
            "-t",
            session_name,
        )
        paste_screen = await _wait_for_tmux_screen(
            session_name,
            lambda screen: "long paste word" in screen
            and "turn:1" in screen,
            description="the bracketed paste to finish in the composer",
        )
        (captures / "paste-composer.txt").write_text(
            paste_screen, encoding="utf-8"
        )
        _tmux("send-keys", "-t", session_name, "-l", "!")
        edited_screen = await _wait_for_tmux_screen(
            session_name,
            lambda screen: "word!" in screen and "turn:1" in screen,
            description="continuing to edit after the paste",
        )
        (captures / "paste-edited.txt").write_text(
            edited_screen, encoding="utf-8"
        )
        _tmux("send-keys", "-t", session_name, "Enter")
        follow_up_screen = await _wait_for_tmux_screen(
            session_name,
            lambda screen: screen.count(REPLY) >= 2,
            description="a long bracketed paste edited before the second turn",
        )
        (captures / "follow-up.txt").write_text(
            follow_up_screen, encoding="utf-8"
        )
        transcript_lines = [line.lstrip() for line in follow_up_screen.splitlines()]
        assert any(line.startswith("❯ follow-up question") for line in transcript_lines), (
            "the follow-up user prompt needs the conversation prompt marker"
        )
        history = await real_client.list_messages(
            session_id,
            THREAD_ID,
            limit=100,
        )
        human_inputs = [
            item.content
            for item in history.items
            if isinstance(item, HumanInputRecord)
        ]
        assert human_inputs[-1] == f"follow-up question\n{long_tail}!"

        other_session = "pty-switch-target"
        await real_client.open_session(
            session_id=other_session,
            thread_id=THREAD_ID,
            mode="new",
        )
        _tmux(
            "send-keys", "-t", session_name, "-l", f"/session {other_session}"
        )
        _tmux("send-keys", "-t", session_name, "Enter")
        other_screen = await _wait_for_tmux_screen(
            session_name,
            lambda screen: f"Switched to {other_session}" in screen,
            description="switching sessions from the real TUI",
        )
        (captures / "switch-away.txt").write_text(other_screen, encoding="utf-8")
        _tmux("send-keys", "-t", session_name, "-l", f"/session {session_id}")
        _tmux("send-keys", "-t", session_name, "Enter")
        restored_screen = await _wait_for_tmux_screen(
            session_name,
            lambda screen: f"session:{session_id}" in screen
            and "follow-up question" in screen
            and REPLY in screen,
            description="returning to the original session and restoring its history",
        )
        (captures / "switch-back.txt").write_text(
            restored_screen, encoding="utf-8"
        )

        await _stop_tmux_tui(session_name)
    finally:
        try:
            _tmux("kill-session", "-t", session_name)
        except AssertionError:
            pass


@pytest.mark.parametrize("base_url", ["thinking_stream"], indirect=True)
async def test_real_cli_tui_pty_shows_thinking_block_while_reasoning_streams(
    base_url: str,
    tmp_path: Path,
) -> None:
    """The thinking block must be visible before the assistant completes."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for the real TTY interaction smoke")

    session_name = f"xbot-think-{uuid.uuid4().hex[:10]}"
    repo_root = Path(__file__).resolve().parents[3]
    xbot = Path(sys.executable).with_name("xbot")
    command = shlex.join([
        str(xbot),
        "tui",
        "--server",
        base_url,
        "--no-plugins",
    ])
    captures = tmp_path / "thinking-pty-captures"
    captures.mkdir()
    try:
        _tmux(
            "new-session",
            "-d",
            "-s",
            session_name,
            "-x",
            "80",
            "-y",
            "24",
            "-c",
            str(repo_root),
            command,
        )
        _resize_tmux_window(session_name, 80, 24)
        await _wait_for_tmux_screen(
            session_name,
            lambda screen: "Ready" in screen,
            description="the real CLI TUI to connect before streaming reasoning",
        )
        _tmux("send-keys", "-t", session_name, "-l", "show your reasoning")
        _tmux("send-keys", "-t", session_name, "Enter")

        streaming = await _wait_for_tmux_screen(
            session_name,
            lambda screen: (
                "Think" in screen
                and "streaming" in screen
                and "First, I am checking" in screen
                and REPLY not in screen
            ),
            description="a visible Think block before the final answer",
        )
        assert len(streaming.splitlines()) == 24
        assert max(map(len, streaming.splitlines()), default=0) <= 80
        assert "✳ Thinking…" not in streaming
        (captures / "thinking-streaming.txt").write_text(
            streaming, encoding="utf-8"
        )
        assert not any("█ █ ▾ Think" in line for line in streaming.splitlines())

        completed = await _wait_for_tmux_screen(
            session_name,
            lambda screen: (
                REPLY in screen
                and "Think" in screen
                and "the request before answering." in screen
                and "show your reasoning" in screen
                and "Ready  turn:1" in screen
            ),
            description="the same Think block after the assistant completes",
        )
        assert "ctx:~" in completed
        assert "in:120" in completed
        assert "out:35" in completed
        assert "cache:20%" in completed
        assert len(completed.splitlines()) == 24
        assert max(map(len, completed.splitlines()), default=0) <= 80
        (captures / "thinking-completed.txt").write_text(
            completed, encoding="utf-8"
        )
        assert completed.count("Think") == 1
        assert "✳ Thinking…" not in completed
        assert "show your reasoning" in completed
        assert "▸ Think" in completed
        assert "ctrl+e expands" in completed
        assert not any("█ █ ▸ Think" in line for line in completed.splitlines())

        _tmux("send-keys", "-t", session_name, "-l", "follow-up after thinking")
        _tmux("send-keys", "-t", session_name, "Enter")
        follow_up = await _wait_for_tmux_screen(
            session_name,
            lambda screen: "follow-up after thinking" in screen
            and REPLY in screen
            and "turn:2" in screen,
            description="a second turn following the streamed-thinking reply",
        )
        (captures / "follow-up-turn.txt").write_text(
            follow_up, encoding="utf-8"
        )
        assert any(
            line.lstrip().startswith("❯ follow-up after thinking")
            for line in follow_up.splitlines()
        ), "the follow-up user turn has the conversation marker at 80x24"
    finally:
        try:
            await _stop_tmux_tui(session_name)
        except AssertionError:
            pass
        try:
            _tmux("kill-session", "-t", session_name)
        except AssertionError:
            pass


@pytest.mark.parametrize("base_url", ["minimax_thinking"], indirect=True)
@pytest.mark.parametrize(
    "terminal_size",
    [
        pytest.param((80, 24), id="80x24"),
        pytest.param((100, 28), id="100x28"),
        pytest.param((120, 40), id="120x40"),
    ],
)
async def test_minimax_thinking_runs_through_provider_server_and_textual_pty(
    base_url: str,
    tmp_path: Path,
    terminal_size: tuple[int, int],
) -> None:
    """Configured provider reasoning must survive the real client/server path."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for the real TTY interaction smoke")

    width, height = terminal_size
    session_name = f"xbot-m3-{width}-{height}-{uuid.uuid4().hex[:8]}"
    repo_root = Path(__file__).resolve().parents[3]
    xbot = Path(sys.executable).with_name("xbot")
    command = shlex.join([
        str(xbot),
        "tui",
        "--server",
        base_url,
        "--no-plugins",
    ])
    captures = tmp_path / "minimax-pty-captures" / f"{width}x{height}"
    captures.mkdir(parents=True)
    try:
        _tmux(
            "new-session",
            "-d",
            "-s",
            session_name,
            "-x",
            str(width),
            "-y",
            str(height),
            "-c",
            str(repo_root),
            command,
        )
        _resize_tmux_window(session_name, width, height)
        await _wait_for_tmux_screen(
            session_name,
            lambda screen: "Ready" in screen,
            description="the real client to attach to the MiniMax-configured server",
        )
        _tmux("send-keys", "-t", session_name, "-l", "explain the next step")
        _tmux("send-keys", "-t", session_name, "Enter")

        streaming = await _wait_for_tmux_screen(
            session_name,
            lambda screen: (
                "Think" in screen
                and "Checking the request." in screen
                and "MiniMax response." not in screen
            ),
            description="provider thinking before its final text arrives",
        )
        (captures / "streaming.txt").write_text(streaming, encoding="utf-8")

        completed = await _wait_for_tmux_screen(
            session_name,
            lambda screen: (
                "Think" in screen
                and "Checking the request." in screen
                and "MiniMax response." in screen
                and "explain the next step" in screen
                and "Ready  turn:1" in screen
            ),
            description="provider reasoning and final text in the completed turn",
        )
        (captures / "completed.txt").write_text(completed, encoding="utf-8")
        assert len(streaming.splitlines()) == height
        assert max(map(cell_len, streaming.splitlines()), default=0) <= width
        assert len(completed.splitlines()) == height
        assert max(map(cell_len, completed.splitlines()), default=0) <= width
        assert completed.count("Think") == 1
        assert "✳ Thinking…" not in completed
        assert "explain the next step" in completed
        assert "▸ Think" in completed
        assert "ctrl+e expands" in completed
        assert not any("█ █ ▸ Think" in line for line in completed.splitlines())

        _tmux("send-keys", "-t", session_name, "C-e")
        expanded = await _wait_for_tmux_screen(
            session_name,
            lambda screen: "▾ Think" in screen
            and "MiniMax response." in screen,
            description="the completed reasoning block expanded in the TTY",
        )
        expanded_lines = expanded.splitlines()
        think_line = next(
            index for index, line in enumerate(expanded_lines) if "▾ Think" in line
        )
        answer_line = next(
            index
            for index, line in enumerate(expanded_lines)
            if "MiniMax response." in line
        )
        assert "Checking the request." in expanded_lines[think_line + 1]
        assert answer_line - think_line == 2, (
            "one reasoning line uses one body row beneath its header; "
            "the block must not reserve the maximum-height window"
        )
        (captures / "think-expanded.txt").write_text(expanded, encoding="utf-8")
        _tmux("send-keys", "-t", session_name, "C-e")
        folded_again = await _wait_for_tmux_screen(
            session_name,
            lambda screen: "▸ Think" in screen
            and "explain the next step" in screen,
            description="the block folded again with the user prompt visible",
        )
        (captures / "think-folded-again.txt").write_text(
            folded_again, encoding="utf-8"
        )

        provider_request = json.loads(
            (tmp_path / "minimax-provider-request.json").read_text(encoding="utf-8")
        )
        assert provider_request["thinking"] == {"type": "adaptive"}
        assert "reasoning_effort" not in provider_request
    finally:
        try:
            await _stop_tmux_tui(session_name)
        except AssertionError:
            pass
        try:
            _tmux("kill-session", "-t", session_name)
        except AssertionError:
            pass


@pytest.mark.parametrize("base_url", ["thinking_activity"], indirect=True)
async def test_real_cli_tui_pty_shows_thinking_activity_without_reasoning(
    base_url: str,
    tmp_path: Path,
) -> None:
    """A slow ordinary turn shows activity without inventing reasoning text."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for the real TTY interaction smoke")

    session_name = f"xbot-work-{uuid.uuid4().hex[:10]}"
    repo_root = Path(__file__).resolve().parents[3]
    xbot = Path(sys.executable).with_name("xbot")
    command = shlex.join([
        str(xbot),
        "tui",
        "--server",
        base_url,
        "--no-plugins",
    ])
    captures = tmp_path / "thinking-activity-pty-captures"
    captures.mkdir()
    try:
        _tmux(
            "new-session",
            "-d",
            "-s",
            session_name,
            "-x",
            "80",
            "-y",
            "24",
            "-c",
            str(repo_root),
            command,
        )
        _resize_tmux_window(session_name, 80, 24)
        await _wait_for_tmux_screen(
            session_name,
            lambda screen: "Ready" in screen,
            description="the real CLI TUI to connect before the slow response",
        )
        _tmux("send-keys", "-t", session_name, "-l", "wait for a normal reply")
        _tmux("send-keys", "-t", session_name, "Enter")

        thinking = await _wait_for_tmux_screen(
            session_name,
            lambda screen: "Thinking" in screen and REPLY not in screen,
            description="a visible thinking activity before any provider text arrives",
        )
        assert "First, I am checking" not in thinking
        assert len(thinking.splitlines()) == 24
        assert max(map(len, thinking.splitlines()), default=0) <= 80
        (captures / "thinking-activity.txt").write_text(
            thinking, encoding="utf-8"
        )

        completed = await _wait_for_tmux_screen(
            session_name,
            lambda screen: REPLY in screen and "Ready  turn:1" in screen,
            description="the normal answer to replace its transient thinking activity",
        )
        assert "Thinking" not in completed
        assert len(completed.splitlines()) == 24
        (captures / "completed.txt").write_text(completed, encoding="utf-8")
    finally:
        try:
            await _stop_tmux_tui(session_name)
        except AssertionError:
            pass
        try:
            _tmux("kill-session", "-t", session_name)
        except AssertionError:
            pass


@pytest.mark.parametrize("base_url", ["core"], indirect=True)
async def test_real_cli_settings_overlay_uses_f2_and_preserves_draft(
    real_client: XBotClient,
    base_url: str,
    tmp_path: Path,
) -> None:
    """Settings navigation and the documented shortcut work in a real TTY."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for the real TTY interaction smoke")

    await real_client.open_session(
        session_id=SESSION_ID,
        thread_id=THREAD_ID,
        mode="new",
    )
    session_name = f"xbot-settings-{uuid.uuid4().hex[:10]}"
    repo_root = Path(__file__).resolve().parents[3]
    xbot = Path(sys.executable).with_name("xbot")
    command = shlex.join([
        str(xbot),
        "tui",
        "--server",
        base_url,
        "--session",
        SESSION_ID,
        "--no-plugins",
    ])
    captures = tmp_path / "settings-pty-captures"
    captures.mkdir()
    try:
        _tmux(
            "new-session",
            "-d",
            "-s",
            session_name,
            "-x",
            "80",
            "-y",
            "24",
            "-c",
            str(repo_root),
            command,
        )
        _resize_tmux_window(session_name, 80, 24)
        await _wait_for_tmux_screen(
            session_name,
            lambda screen: "Ready" in screen and f"session:{SESSION_ID}" in screen,
            description="the settings TUI to attach to the existing session",
        )

        _tmux("send-keys", "-t", session_name, "-l", "/status")
        _tmux("send-keys", "-t", session_name, "Enter")
        report = await _wait_for_tmux_screen(
            session_name,
            lambda screen: "Status" in screen and "ID: tui-e2e" in screen,
            description="the local read-only Status page",
        )
        (captures / "status-command.txt").write_text(report, encoding="utf-8")
        assert "Settings" not in report, "/status is read-only rather than settings navigation"
        _tmux("send-keys", "-t", session_name, "Escape")
        await _wait_for_tmux_screen(
            session_name,
            lambda screen: "Status" not in screen,
            description="closing the read-only Status page",
        )

        _tmux("send-keys", "-t", session_name, "-l", "unsent draft")
        _tmux("send-keys", "-t", session_name, "F2")
        status = await _wait_for_tmux_screen(
            session_name,
            lambda screen: "Settings" in screen and "ID: tui-e2e" in screen,
            description="the Status settings page opened with F2",
        )
        (captures / "status.txt").write_text(status, encoding="utf-8")

        _tmux("send-keys", "-t", session_name, "Down")
        _tmux("send-keys", "-t", session_name, "Enter")
        model = await _wait_for_tmux_screen(
            session_name,
            lambda screen: "Settings" in screen
            and "Provider: default" in screen
            and "Change model" in screen,
            description="the Model page backed by the provider catalog",
        )
        (captures / "model.txt").write_text(model, encoding="utf-8")
        model_action_rows = [
            line
            for line in model.splitlines()
            if any(
                label in line
                for label in (
                    "Change provider",
                    "Change model",
                    "Change effort",
                    "Change agent",
                )
            )
        ]
        assert len(model_action_rows) == 4
        assert all(
            "▔" not in line and "▁" not in line
            for line in model_action_rows
        ), (
            "Settings actions should render as compact rows; dialog navigation "
            "borders are outside the action rows"
        )

        _tmux("send-keys", "-t", session_name, "Escape")
        closed = await _wait_for_tmux_screen(
            session_name,
            lambda screen: "Settings" not in screen and "unsent draft" in screen,
            description="closing Settings without losing the composer draft",
        )
        (captures / "closed.txt").write_text(closed, encoding="utf-8")
    finally:
        try:
            await _stop_tmux_tui(session_name)
        except AssertionError:
            pass
        try:
            _tmux("kill-session", "-t", session_name)
        except AssertionError:
            pass


@pytest.mark.parametrize("base_url", ["queue_stream"], indirect=True)
async def test_real_cli_enter_queues_during_a_running_turn_and_renders_the_queued_prompt(
    real_client: XBotClient,
    base_url: str,
    tmp_path: Path,
) -> None:
    """The key gesture, server queue, and compact visible strip must agree."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for the real TTY interaction smoke")

    session_name = f"xbot-queue-{uuid.uuid4().hex[:10]}"
    repo_root = Path(__file__).resolve().parents[3]
    xbot = Path(sys.executable).with_name("xbot")
    command = shlex.join([
        str(xbot),
        "tui",
        "--server",
        base_url,
        "--session",
        SESSION_ID,
        "--no-plugins",
    ])
    captures = tmp_path / "queue-pty-captures"
    captures.mkdir()
    try:
        await real_client.open_session(
            session_id=SESSION_ID,
            thread_id=THREAD_ID,
            mode="new",
        )
        _tmux(
            "new-session",
            "-d",
            "-s",
            session_name,
            "-x",
            "80",
            "-y",
            "24",
            "-c",
            str(repo_root),
            command,
        )
        _resize_tmux_window(session_name, 80, 24)
        await _wait_for_tmux_screen(
            session_name,
            lambda screen: "Ready" in screen,
            description="the real CLI TUI to connect before queueing",
        )
        _tmux("send-keys", "-t", session_name, "-l", "first request")
        _tmux("send-keys", "-t", session_name, "Enter")
        await _wait_for_tmux_screen(
            session_name,
            lambda screen: "Running" in screen and "turn:1" in screen,
            description="the first provider turn to be running",
        )

        _tmux("send-keys", "-t", session_name, "-l", "queued follow-up")
        _tmux("send-keys", "-t", session_name, "Enter")
        strip = await _wait_for_tmux_screen(
            session_name,
            lambda screen: (
                "queued follow-up" in screen
                and "next-turn" in screen
                and "Running" in screen
            ),
            description="the queued prompt visible above the composer during the first turn",
        )
        (captures / "queue-strip.txt").write_text(strip, encoding="utf-8")
        pending = await real_client.list_pending_inputs(SESSION_ID, THREAD_ID)
        assert [item.content for item in pending.items] == ["queued follow-up"]
        assert "queued follow-up" in strip

        _tmux("send-keys", "-t", session_name, "-l", "steer request")
        _tmux("send-keys", "-t", session_name, "M-s")
        deadline = asyncio.get_running_loop().time() + 5.0
        while True:
            pending = await real_client.list_pending_inputs(SESSION_ID, THREAD_ID)
            if len(pending.items) == 2:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(
                    "Alt+S did not add a next-step input; "
                    f"pending={pending.items!r}\nscreen:\n{_capture_tmux_screen(session_name)}"
                )
            await asyncio.sleep(0.05)
        assert [(item.content, item.target) for item in pending.items] == [
            ("steer request", "next-step"),
            ("queued follow-up", "next-turn"),
        ]
        steered = await _wait_for_tmux_screen(
            session_name,
            lambda screen: (
                "steer request" in screen
                and "queued follow-up" in screen
                and "Alt+S steer" in screen
            ),
            description="the accepted Alt+S steer alongside the queued input",
        )
        (captures / "queue-and-steer.txt").write_text(steered, encoding="utf-8")
        assert "Alt+S steer" in steered
    finally:
        try:
            await _stop_tmux_tui(session_name)
        except AssertionError:
            pass
        try:
            _tmux("kill-session", "-t", session_name)
        except AssertionError:
            pass


@pytest.mark.parametrize("base_url", ["core"], indirect=True)
async def test_two_real_cli_tuis_keep_sessions_isolated_and_resume_from_disk(
    real_client: XBotClient,
    base_url: str,
    tmp_path: Path,
) -> None:
    """Two installed TUI processes share one server without sharing a session."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for the real TTY interaction smoke")

    session_a = "pty-concurrent-a"
    session_b = "pty-concurrent-b"
    await real_client.open_session(
        session_id=session_a, thread_id=THREAD_ID, mode="new"
    )
    await real_client.open_session(
        session_id=session_b, thread_id=THREAD_ID, mode="new"
    )

    repo_root = Path(__file__).resolve().parents[3]
    xbot = Path(sys.executable).with_name("xbot")
    tmux_a = f"xbot-a-{uuid.uuid4().hex[:10]}"
    tmux_b = f"xbot-b-{uuid.uuid4().hex[:10]}"
    tmux_resumed = f"xbot-resume-{uuid.uuid4().hex[:10]}"
    live_tmux = {tmux_a, tmux_b}
    captures = tmp_path / "multi-pty-captures"
    captures.mkdir()

    def command(session_id: str) -> str:
        return shlex.join([
            str(xbot),
            "tui",
            "--server",
            base_url,
            "--session",
            session_id,
            "--thread",
            THREAD_ID,
            "--workspace",
            str(repo_root),
            "--no-plugins",
        ])

    def launch(tmux_name: str, session_id: str) -> None:
        _tmux(
            "new-session",
            "-d",
            "-s",
            tmux_name,
            "-x",
            "120",
            "-y",
            "36",
            "-c",
            str(repo_root),
            command(session_id),
        )
        _resize_tmux_window(tmux_name, 120, 36)

    try:
        launch(tmux_a, session_a)
        launch(tmux_b, session_b)
        for name, session_id in ((tmux_a, session_a), (tmux_b, session_b)):
            screen = await _wait_for_tmux_screen(
                name,
                lambda value, session_id=session_id: (
                    "Ready" in value and f"session:{session_id}" in value
                ),
                description=f"{session_id} to attach to the shared server",
            )
            (captures / f"{session_id}-ready.txt").write_text(
                screen, encoding="utf-8"
            )

        _tmux("send-keys", "-t", tmux_a, "-l", "alpha first turn")
        _tmux("send-keys", "-t", tmux_a, "Enter")
        alpha = await _wait_for_tmux_screen(
            tmux_a,
            lambda screen: (
                "alpha first turn" in screen
                and REPLY in screen
                and "Think" in screen
                and "Ready  turn:1" in screen
            ),
            description="session A's first completed turn",
        )
        (captures / "session-a-turn-1.txt").write_text(alpha, encoding="utf-8")

        _tmux("send-keys", "-t", tmux_a, "C-e")
        expanded = await _wait_for_tmux_screen(
            tmux_a,
            lambda screen: "ctrl+e collapses" in screen,
            description="the real reasoning block to expand",
        )
        (captures / "session-a-expanded.txt").write_text(
            expanded, encoding="utf-8"
        )
        _tmux("send-keys", "-t", tmux_a, "C-e")

        _tmux("send-keys", "-t", tmux_b, "-l", "beta independent turn")
        _tmux("send-keys", "-t", tmux_b, "Enter")
        beta = await _wait_for_tmux_screen(
            tmux_b,
            lambda screen: (
                "beta independent turn" in screen
                and REPLY in screen
                and "Ready  turn:1" in screen
            ),
            description="session B's independent turn",
        )
        (captures / "session-b-turn-1.txt").write_text(beta, encoding="utf-8")
        assert "beta independent turn" not in _capture_tmux_screen(tmux_a)
        assert "alpha first turn" not in beta

        await _stop_tmux_tui(tmux_a)
        live_tmux.discard(tmux_a)
        await real_client.close_session(session_a)
        listed = {item.session_id: item for item in (await real_client.list_sessions()).sessions}
        assert listed[session_a].status == "inactive"
        assert listed[session_b].status == "active"

        launch(tmux_resumed, session_a)
        live_tmux.add(tmux_resumed)
        resumed = await _wait_for_tmux_screen(
            tmux_resumed,
            lambda screen: (
                f"session:{session_a}" in screen
                and "alpha first turn" in screen
                and REPLY in screen
                and "Ready  turn:1" in screen
            ),
            description="a new TUI process to hydrate session A from disk",
        )
        (captures / "session-a-resumed.txt").write_text(
            resumed, encoding="utf-8"
        )

        _tmux("send-keys", "-t", tmux_resumed, "-l", "alpha after resume")
        _tmux("send-keys", "-t", tmux_resumed, "Enter")
        resumed_turn = await _wait_for_tmux_screen(
            tmux_resumed,
            lambda screen: (
                "alpha after resume" in screen
                and screen.count(REPLY) == 2
                and "Ready  turn:2" in screen
            ),
            description="continued conversation after disk resume",
        )
        (captures / "session-a-turn-2.txt").write_text(
            resumed_turn, encoding="utf-8"
        )
        beta_still_live = _capture_tmux_screen(tmux_b)
        (captures / "session-b-still-live.txt").write_text(
            beta_still_live, encoding="utf-8"
        )
        assert "beta independent turn" in beta_still_live
        assert "alpha after resume" not in beta_still_live

        history_a = await real_client.list_messages(session_a, THREAD_ID, limit=100)
        history_b = await real_client.list_messages(session_b, THREAD_ID, limit=100)
        assert [
            item.content for item in history_a.items if isinstance(item, HumanInputRecord)
        ] == ["alpha first turn", "alpha after resume"]
        assert [
            item.content for item in history_b.items if isinstance(item, HumanInputRecord)
        ] == ["beta independent turn"]
        assert sum(isinstance(item, AssistantRecord) for item in history_a.items) == 2
        assert sum(isinstance(item, AssistantRecord) for item in history_b.items) == 1

        await _stop_tmux_tui(tmux_resumed)
        live_tmux.discard(tmux_resumed)
        await _stop_tmux_tui(tmux_b)
        live_tmux.discard(tmux_b)
    finally:
        for name in live_tmux:
            try:
                _tmux("kill-session", "-t", name)
            except AssertionError:
                pass


@pytest.mark.parametrize("base_url", ["core"], indirect=True)
async def test_exiting_real_tui_before_input_does_not_persist_empty_session(
    real_client: XBotClient,
    tmp_path: Path,
) -> None:
    session_root = RuntimePaths.from_data_dir(tmp_path / "data").session(
        SESSION_ID
    ).root
    app = tui(real_client)

    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        assert not session_root.exists()
        await pilot.press("ctrl+c")

    assert not session_root.exists()
    listed = await real_client.list_sessions()
    assert SESSION_ID not in {summary.session_id for summary in listed.sessions}


async def test_the_real_client_connects_and_reports_ready(real_client: XBotClient) -> None:
    app = tui(real_client)
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        assert app.controller is not None
        assert app.controller.state.session_id == SESSION_ID
        assert app.controller.state.thread_id == THREAD_ID


async def test_the_real_thread_read_is_the_authority_on_the_turn(
    real_client: XBotClient,
) -> None:
    app = tui(real_client)
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        # The watchdog already read the thread; a second read must agree.
        assert app.controller is not None
        assert await app.controller.transport.watchdog_once() is True


# --- a real turn ----------------------------------------------------------


async def test_a_real_turn_arrives_on_screen(real_client: XBotClient) -> None:
    app = tui(real_client)
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        composer = app.query_one("#composer", Composer)
        composer.load_text("say hello")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: REPLY in transcript_text(app),
            description="the model's reply",
        )
        assert "say hello" in transcript_text(app), "the prompt is shown too"
        assert app.controller is not None
        assert app.controller.state.timeline.get("") is None


@pytest.mark.parametrize("base_url", ["caption"], indirect=True)
async def test_caption_updates_the_real_tui_statusline(real_client: XBotClient) -> None:
    app = tui(real_client)
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        app.query_one("#composer", Composer).load_text("plan a trip to Kyoto")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: REPLY in transcript_text(app)
            or CAPTION in transcript_text(app)
            or "error" in status_text(app).lower(),
            description="the model reply after captioning",
        )
        assert REPLY in transcript_text(app), (
            f"status={status_text(app)!r} transcript={transcript_text(app)!r}"
        )
        assert app.controller is not None
        assert await app.controller.transport.watchdog_once() is True
        await pilot.pause()

        assert app.controller.state.title == CAPTION
        assert CAPTION in status_text(app)

        # Switching away and back must adopt the persisted thread title into
        # both state and the bottom statusline, not just retain a stale label.
        await real_client.open_session(
            session_id="other-caption-e2e", thread_id=THREAD_ID, mode="new"
        )
        app.query_one("#composer", Composer).load_text("/session other-caption-e2e")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: app.controller is not None
            and app.controller.state.session_id == "other-caption-e2e",
            description="switch away from the captioned session",
        )
        assert app.controller is not None
        assert app.controller.state.title == "other-caption-e2e"

        app.query_one("#composer", Composer).load_text(f"/session {SESSION_ID}")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: app.controller is not None
            and app.controller.state.session_id == SESSION_ID
            and app.controller.state.title == CAPTION,
            description="restore the persisted caption after switching back",
        )
        assert CAPTION in status_text(app)

    await real_client.close_session(SESSION_ID)
    stored = await real_client.get_session(SESSION_ID)
    assert stored.title == CAPTION
    resumed = await real_client.open_session(
        session_id=SESSION_ID,
        thread_id=THREAD_ID,
        mode="resume",
    )
    assert resumed.data.metadata.title == CAPTION


@pytest.mark.parametrize("base_url", ["caption_retry"], indirect=True)
async def test_caption_retries_after_a_real_first_turn_provider_failure(
    real_client: XBotClient,
) -> None:
    app = tui(real_client)
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        composer = app.query_one("#composer", Composer)
        composer.load_text("first question")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: REPLY in transcript_text(app),
            description="first answer despite caption provider failure",
        )
        assert app.controller is not None
        assert app.controller.state.title == SESSION_ID

        composer.load_text("follow-up question")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: transcript_text(app).count(REPLY) == 2,
            description="follow-up answer after caption retry",
        )
        assert await app.controller.transport.watchdog_once() is True
        await pilot.pause()
        assert app.controller.state.title == CAPTION
        assert CAPTION in status_text(app)

    await real_client.close_session(SESSION_ID)
    stored = await real_client.get_session(SESSION_ID)
    assert stored.title == CAPTION
    resumed = await real_client.open_session(
        session_id=SESSION_ID,
        thread_id=THREAD_ID,
        mode="resume",
    )
    assert resumed.data.metadata.title == CAPTION


@pytest.mark.parametrize("base_url", ["permission"], indirect=True)
async def test_real_permission_request_can_be_approved_from_the_tui(
    real_client: XBotClient,
    tmp_path: Path,
) -> None:
    app = tui(real_client)
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        composer = app.query_one("#composer", Composer)
        composer.load_text("write a file after asking permission")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: bool(app.controller and app.controller.state.pending_interactions),
            description="the edit permission request",
        )
        assert app.controller is not None
        request = next(iter(app.controller.state.pending_interactions.values()))
        assert isinstance(request, PermissionRequest)
        assert f"ID: {request.interaction_id}" in transcript_text(app)
        from XBotv2.tui.view.selection import SelectionScreen

        await wait_for(
            pilot,
            lambda: isinstance(app.screen, SelectionScreen),
            description="the permission chooser",
        )
        chooser = app.screen
        assert isinstance(chooser, SelectionScreen)
        assert chooser.rendered_rows == (
            "▸ Allow once",
            "  Allow for this session",
            "  Deny",
        )
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: REPLY in transcript_text(app),
            description="the model reply after permission approval",
        )
        assert not app.controller.state.pending_interactions
        assert (tmp_path / "workspace" / "approval.txt").read_text(
            encoding="utf-8"
        ) == "approved by the user"
        assert app.screen.focused is composer.input


@pytest.mark.parametrize("base_url", ["permission_deny"], indirect=True)
async def test_real_permission_request_can_be_denied_from_the_tui(
    real_client: XBotClient,
    tmp_path: Path,
) -> None:
    app = tui(real_client)
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        composer = app.query_one("#composer", Composer)
        composer.load_text("write only if the user approves")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: bool(app.controller and app.controller.state.pending_interactions),
            description="the edit permission request",
        )
        assert app.controller is not None
        request = next(iter(app.controller.state.pending_interactions.values()))
        assert isinstance(request, PermissionRequest)
        assert f"ID: {request.interaction_id}" in transcript_text(app)
        from XBotv2.tui.view.selection import SelectionScreen

        await wait_for(
            pilot,
            lambda: isinstance(app.screen, SelectionScreen),
            description="the permission chooser",
        )
        await pilot.press("down", "down", "enter")
        await wait_for(
            pilot,
            lambda: REPLY in transcript_text(app),
            description="the model reply after permission denial",
        )
        assert not app.controller.state.pending_interactions
        assert "→ denied" in transcript_text(app)
        assert not (tmp_path / "workspace" / "denied.txt").exists()
        assert app.screen.focused is composer.input


@pytest.mark.parametrize("base_url", ["interaction"], indirect=True)
async def test_real_user_input_request_can_be_answered_from_the_tui(
    real_client: XBotClient,
) -> None:
    app = tui(real_client)
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        composer = app.query_one("#composer", Composer)
        composer.load_text("ask me where to deploy")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: bool(app.controller and app.controller.state.pending_interactions),
            description="the permission request for ask_user",
        )
        assert app.controller is not None
        permission = next(iter(app.controller.state.pending_interactions.values()))
        assert isinstance(permission, PermissionRequest)
        assert f"ID: {permission.interaction_id}" in transcript_text(app)
        from XBotv2.tui.view.selection import SelectionScreen

        await wait_for(
            pilot,
            lambda: isinstance(app.screen, SelectionScreen),
            description="the permission chooser for ask_user",
        )
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: any(
                isinstance(request, UserInputRequest)
                for request in app.controller.state.pending_interactions.values()
            ),
            description="the user-input question after approval",
        )
        question = next(
            request
            for request in app.controller.state.pending_interactions.values()
            if isinstance(request, UserInputRequest)
        )
        assert f"ID: {question.interaction_id}" in transcript_text(app)
        await wait_for(
            pilot,
            lambda: isinstance(app.screen, SelectionScreen)
            and app.screen.title_text == "Question",
            description="the user-input option chooser",
        )
        assert "Which deployment window?" in app.screen.description_text
        await pilot.press("down", "enter")
        await wait_for(
            pilot,
            lambda: REPLY in transcript_text(app),
            description="the model reply after receiving the answer",
        )
        assert not app.controller.state.pending_interactions
        assert app.screen.focused is composer.input


async def test_real_reasoning_and_usage_reach_the_tui(real_client: XBotClient) -> None:
    app = tui(real_client)
    async with app.run_test(size=(120, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        app.query_one("#composer", Composer).load_text("show reasoning and usage")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: REPLY in transcript_text(app),
            description="the completed answer",
        )

        transcript = transcript_text(app)
        assert "I am checking the request before answering." in transcript
        assert "Think" in transcript
        status = status_text(app)
        assert "in:120" in status
        assert "out:35" in status
        assert "cache:20%" in status
        assert "ctx:~" in status
        assert "session:tui-e2e" in status
        assert "ctx:~" in status
        assert "/4096" in status


@pytest.mark.parametrize("base_url", ["compact"], indirect=True)
async def test_real_tui_compact_command_renders_event_and_persists_summary_trajectory(
    real_client: XBotClient,
) -> None:
    """The TUI command, event stream, transcript and durable history agree."""
    app = tui(real_client)
    async with app.run_test(size=(120, 30)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        await wait_for(
            pilot,
            lambda: app.commands.get("compact") is not None,
            description="the server-owned compact command in the TUI catalogue",
        )

        composer = app.query_one("#composer", Composer)
        for index in range(5):
            composer.load_text(f"question {index}")
            await pilot.press("enter")
            try:
                await wait_for(
                    pilot,
                    lambda index=index: f"answer {index}" in transcript_text(app)
                    and "Ready" in status_text(app),
                    description=f"turn {index} to finish",
                )
            except AssertionError as exc:
                raise AssertionError(
                    f"{exc}; status={status_text(app)!r}; "
                    f"transcript={transcript_text(app)!r}"
                ) from exc

        composer.load_text("/compact")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: "Conversation compacted" in transcript_text(app)
            and "summary of the earlier discussion" in transcript_text(app),
            description="the live compaction event and summary in the transcript",
        )

        sessions = await real_client.list_sessions()
        assert len(sessions.sessions) == 1
        session_id = sessions.sessions[0].session_id
        trajectory = await real_client.list_trajectory(
            session_id, THREAD_ID, limit=100
        )
        replacements = [
            entry
            for entry in trajectory.page.items
            if isinstance(entry, SurfaceReplaced)
        ]
        summaries = [
            item
            for replacement in replacements
            for item in replacement.replacements
            if isinstance(item, CompactionSummaryMessage)
        ]
        assert len(summaries) == 1
        assert "summary of the earlier discussion" in summaries[0].summary


@pytest.mark.parametrize("base_url", ["compact"], indirect=True)
async def test_real_cli_compaction_survives_process_resume_and_continues(
    real_client: XBotClient,
    base_url: str,
    tmp_path: Path,
) -> None:
    """One terminal performs compaction; a new process resumes and continues."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for the real TTY interaction smoke")

    repo_root = Path(__file__).resolve().parents[3]
    xbot = Path(sys.executable).with_name("xbot")
    first = f"xbot-compact-{uuid.uuid4().hex[:10]}"
    resumed_name = f"xbot-compact-resume-{uuid.uuid4().hex[:10]}"
    live = {first}
    captures = tmp_path / "compact-resume-pty-captures"
    captures.mkdir()

    def command(session_id: str | None = None) -> str:
        values = [str(xbot), "tui", "--server", base_url, "--no-plugins"]
        if session_id is not None:
            values.extend(("--session", session_id, "--thread", THREAD_ID))
        return shlex.join(values)

    def launch(name: str, session_id: str | None = None) -> None:
        _tmux(
            "new-session", "-d", "-s", name, "-x", "120", "-y", "36",
            "-c", str(repo_root), command(session_id),
        )
        _resize_tmux_window(name, 120, 36)

    try:
        launch(first)
        await _wait_for_tmux_screen(
            first, lambda screen: "Ready" in screen,
            description="the compact TUI to attach",
        )
        for index in range(5):
            _tmux("send-keys", "-t", first, "-l", f"question {index}")
            _tmux("send-keys", "-t", first, "Enter")
            await _wait_for_tmux_screen(
                first,
                lambda screen, index=index: (
                    f"answer {index}" in screen and f"Ready  turn:{index + 1}" in screen
                ),
                description=f"turn {index + 1} before compaction",
            )

        _tmux("send-keys", "-t", first, "-l", "/compact")
        _tmux("send-keys", "-t", first, "Enter")
        compacted = await _wait_for_tmux_screen(
            first,
            lambda screen: (
                "Conversation compacted" in screen
                and "summary of the earlier discussion" in screen
            ),
            description="the compacted transcript in the first TUI process",
        )
        (captures / "compacted.txt").write_text(compacted, encoding="utf-8")

        sessions = await real_client.list_sessions()
        session_id = sessions.sessions[0].session_id
        await _stop_tmux_tui(first)
        live.discard(first)
        await real_client.close_session(session_id)

        launch(resumed_name, session_id)
        live.add(resumed_name)
        resumed = await _wait_for_tmux_screen(
            resumed_name,
            lambda screen: (
                "question 4" in screen
                and "Ready  turn:5" in screen
            ),
            description="the preserved transcript and lifetime turn count after resume",
        )
        (captures / "resumed.txt").write_text(resumed, encoding="utf-8")

        _tmux("send-keys", "-t", resumed_name, "-l", "continue after compact")
        _tmux("send-keys", "-t", resumed_name, "Enter")
        continued = await _wait_for_tmux_screen(
            resumed_name,
            lambda screen: (
                "answer after compact resume" in screen and "Ready  turn:6" in screen
            ),
            description="a completed turn after compact process resume",
        )
        (captures / "continued.txt").write_text(continued, encoding="utf-8")

        history = await real_client.list_messages(session_id, THREAD_ID, limit=100)
        assert any(
            isinstance(item, HumanInputRecord)
            and item.content == "continue after compact"
            for item in history.items
        )
        assert any(
            isinstance(item, AssistantRecord)
            and item.content == "answer after compact resume"
            for item in history.items
        )
        trajectory = await real_client.list_trajectory(session_id, THREAD_ID, limit=100)
        assert any(isinstance(item, SurfaceReplaced) for item in trajectory.page.items)

        await _stop_tmux_tui(resumed_name)
        live.discard(resumed_name)
    finally:
        for name in live:
            try:
                _tmux("kill-session", "-t", name)
            except AssertionError:
                pass


async def test_a_real_turn_ends_ready_and_shows_no_pending_marker(
    real_client: XBotClient,
) -> None:
    app = tui(real_client)
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        composer = app.query_one("#composer", Composer)
        composer.load_text("say hello")
        await pilot.press("enter")
        await wait_for(pilot, lambda: REPLY in transcript_text(app), description="reply")
        await wait_for(
            pilot,
            lambda: "Running" not in status_text(app),
            description="the turn to finish",
        )
        assert "Ready" in status_text(app), (
            f"status={status_text(app)!r} transcript={transcript_text(app)!r}"
        )
        assert app.controller is not None
        assert "sending" not in transcript_text(app), (
            transcript_text(app)
            + "\nSTATE="
            + repr([(e.id, e.kind.value, getattr(e, "content", "")) for e in app.controller.state.timeline])
        )


async def test_the_real_transcript_is_ordered(real_client: XBotClient) -> None:
    """Prompt, then reply -- and the reply is not written into the prompt."""
    app = tui(real_client)
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        composer = app.query_one("#composer", Composer)
        composer.load_text("first question")
        await pilot.press("enter")
        await wait_for(pilot, lambda: REPLY in transcript_text(app), description="reply")
        rendered = transcript_text(app)
        assert rendered.index("first question") < rendered.index(REPLY)
        assert app.controller is not None
        assert [
            getattr(entry, "content", "")
            for entry in app.controller.state.timeline
            if getattr(entry, "content", "")
        ] == ["first question", REPLY]


async def test_the_real_sequence_has_no_gaps(real_client: XBotClient) -> None:
    """A real stream must arrive complete: no gap notice, no rejected frame."""
    from XBotv2.tui.timeline import ErrorEntry, NoticeEntry

    app = tui(real_client)
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        assert app.controller is not None
        composer = app.query_one("#composer", Composer)
        composer.load_text("say hello")
        await pilot.press("enter")
        await wait_for(pilot, lambda: REPLY in transcript_text(app), description="reply")
        await wait_for(
            pilot,
            lambda: "Running" not in status_text(app),
            description="the turn to finish",
        )
        timeline = app.controller.state.timeline
        assert not [e for e in timeline if isinstance(e, ErrorEntry)]
        assert not [
            e for e in timeline
            if isinstance(e, NoticeEntry) and e.notice_kind == "stream_gap"
        ]


async def test_two_real_turns_in_a_row(real_client: XBotClient) -> None:
    """The second turn must not reuse the first turn's entry."""
    app = tui(real_client)
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        composer = app.query_one("#composer", Composer)
        for index in (1, 2):
            composer.load_text(f"question {index}")
            await pilot.press("enter")
            await wait_for(
                pilot,
                lambda: transcript_text(app).count(REPLY) >= index,
                description=f"reply {index}",
            )
        assert app.controller is not None
        contents = [
            getattr(entry, "content", "")
            for entry in app.controller.state.timeline
            if getattr(entry, "content", "")
        ]
        assert contents == ["question 1", REPLY, "question 2", REPLY]


async def test_switching_sessions_swaps_the_transcript_and_keeps_chatting(
    real_client: XBotClient,
) -> None:
    """The capability the deleted transport test covered: resume another session
    mid-run over a real socket and continue the conversation there."""
    app = tui(real_client)
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        composer = app.query_one("#composer", Composer)

        # A turn in the session the TUI attached to.
        composer.load_text("hello A")
        await pilot.press("enter")
        await wait_for(pilot, lambda: REPLY in transcript_text(app), description="reply A")
        assert "hello A" in transcript_text(app)

        # A second session with its own history, created through the same client.
        opened = await real_client.open_session(
            session_id="other-e2e", thread_id=THREAD_ID, mode="new"
        )
        await real_client.send_message(
            "other-e2e", THREAD_ID, "hello B", request_id="b-1"
        )
        async for frame in real_client.stream_events(
            "other-e2e", THREAD_ID, after=opened.data.event_cursor
        ):
            if frame.kind == "turn_ended":
                break

        # Switch from the TUI.
        composer.load_text("/session other-e2e")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: "hello B" in transcript_text(app),
            description="the other session's history",
        )
        assert app.controller is not None
        assert app.controller.state.session_id == "other-e2e"
        assert "hello A" not in transcript_text(app), "the transcript was replaced"
        assert "Switched to other-e2e" in transcript_text(app)

        # And keep chatting in the new session.
        composer.load_text("and again")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: transcript_text(app).count(REPLY) >= 2,
            description="a reply in the new session",
        )
        assert transcript_text(app).count(REPLY) == 2


async def test_an_attached_image_reaches_the_real_server(
    real_client: XBotClient, tmp_path: Path
) -> None:
    """Attaching is only useful if the bytes arrive: check the stored message."""
    path = tmp_path / "shot.png"
    path.write_bytes(PNG_BYTES)
    app = tui(real_client)
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        composer = app.query_one("#composer", Composer)
        composer.load_text(f"/attach {path}")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: f"Attached {path.name}" in transcript_text(app),
            description="the attachment to be held",
        )
        composer.load_text("what is this")
        await pilot.press("enter")
        await wait_for(pilot, lambda: REPLY in transcript_text(app), description="reply")
        stored = await real_client.list_messages(SESSION_ID, THREAD_ID)
        user = [item for item in stored.items if item.kind == "human_input"]
        assert len(user) == 1, "one prompt was sent"
        assert user[0].content == "what is this"
        assert [image.media_type for image in user[0].images] == ["image/png"]


async def screen_text(app: TuiApp) -> str:
    """Everything the current screen is showing (the picker, usually)."""
    parts: list[str] = []
    for widget in app.screen.query("Static"):
        content = getattr(widget, "content", None)
        parts.append(str(getattr(content, "plain", "") or ""))
    return "\n".join(parts)


# --- /thread over the real HTTP contract ----------------------------------


async def test_the_real_thread_picker_lists_the_sessions_threads(
    real_client: XBotClient,
) -> None:
    from XBotv2.tui.view.selection import SelectionScreen

    app = tui(real_client)
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        composer = app.query_one("#composer", Composer)
        composer.load_text("/thread")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: isinstance(app.screen, SelectionScreen),
            description="the thread picker",
        )
        assert THREAD_ID in await screen_text(app)
        assert "main" in await screen_text(app), "the picker says which thread is main"


async def test_switching_to_the_real_main_thread_keeps_the_client_writable(
    real_client: XBotClient,
) -> None:
    """A thread switch is a re-attach, not a second view: the client keeps working."""
    app = tui(real_client)
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        composer = app.query_one("#composer", Composer)
        composer.load_text("/thread main")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: "Back on the main thread" in transcript_text(app),
            description="the switch notice",
        )
        assert "subagent:" not in status_text(app), "the main thread is not a subagent view"
        composer.load_text("still talking")
        await pilot.press("enter")
        await wait_for(pilot, lambda: REPLY in transcript_text(app), description="reply")
        stored = await real_client.list_messages(SESSION_ID, THREAD_ID)
        assert [item.content for item in stored.items if item.kind == "human_input"] == [
            "still talking"
        ]


@pytest.mark.parametrize("base_url", ["subagent"], indirect=True)
async def test_real_subagent_can_be_inspected_read_only_and_returns_to_main(
    real_client: XBotClient,
) -> None:
    from XBotv2.tui.view.selection import SelectionScreen

    app = tui(real_client)
    async with app.run_test(size=(100, 28)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        composer = app.query_one("#composer", Composer)
        composer.load_text("Delegate this review and wait for the result")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: "The delegated review is complete." in transcript_text(app),
            description="the parent completion after the subagent",
        )

        child_id = ""
        deadline = asyncio.get_running_loop().time() + 8
        while asyncio.get_running_loop().time() < deadline:
            threads = (await real_client.list_threads(SESSION_ID)).threads
            child = next((item for item in threads if item.kind == "subagent"), None)
            if child is not None:
                child_id = child.thread_id
                break
            await asyncio.sleep(0.02)
        assert child_id, "the production subagent created a public child thread"

        composer.load_text("parent draft survives inspection")
        await pilot.press("ctrl+t")
        await wait_for(
            pilot,
            lambda: isinstance(app.screen, SelectionScreen),
            description="the agent thread picker",
        )
        assert child_id in await screen_text(app)
        child_index = next(
            index
            for index, option in enumerate(app.screen.model.options)
            if option.value == child_id
        )
        for _ in range(child_index):
            await pilot.press("down")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: "The reviewer found no blocking issue." in transcript_text(app),
            description="the child transcript",
        )

        assert app.controller is not None
        assert app.controller.state.thread_id == child_id
        assert app.controller.state.read_only
        assert composer.text == "parent draft survives inspection"
        assert f"subagent:{child_id}" in status_text(app)

        await pilot.press("escape")
        await wait_for(
            pilot,
            lambda: app.controller is not None and not app.controller.state.read_only,
            description="returning to the main thread",
        )
        assert app.controller.state.thread_id == THREAD_ID
        assert composer.text == "parent draft survives inspection"
        assert "The delegated review is complete." in transcript_text(app)

    resumed = tui_app(
        real_client,
        config=TransportConfig(
            session_id=SESSION_ID,
            thread_id=THREAD_ID,
            mode="resume",
            reconnect_delays=(0.05, 0.1, 0.25),
        ),
        render_interval=0.01,
    )
    async with resumed.run_test(size=(100, 28)) as pilot:
        await wait_for(
            pilot,
            lambda: "Ready" in status_text(resumed),
            description="the resumed main thread",
        )
        assert "The delegated review is complete." in transcript_text(resumed)

        await pilot.press("ctrl+t")
        await wait_for(
            pilot,
            lambda: isinstance(resumed.screen, SelectionScreen),
            description="the resumed agent thread picker",
        )
        child_index = next(
            index
            for index, option in enumerate(resumed.screen.model.options)
            if option.value == child_id
        )
        for _ in range(child_index):
            await pilot.press("down")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: "The reviewer found no blocking issue." in transcript_text(resumed),
            description="the resumed child transcript",
        )
        assert resumed.controller is not None
        assert resumed.controller.state.read_only
        assert resumed.controller.state.thread_id == child_id


@pytest.mark.parametrize("base_url", ["subagent_running"], indirect=True)
async def test_second_real_tui_hydrates_an_already_running_subagent(
    real_client: XBotClient,
) -> None:
    first = tui(real_client)
    async with first.run_test(size=(100, 28)) as first_pilot:
        await wait_for(
            first_pilot,
            lambda: "Ready" in status_text(first),
            description="the first TUI",
        )
        first.query_one("#composer", Composer).load_text("Delegate this in background")
        await first_pilot.press("enter")
        await wait_for(
            first_pilot,
            lambda: (
                "The delegated review is running in the background."
                in transcript_text(first)
                and bool(first.controller and first.controller.state.jobs)
            ),
            description="the live background subagent",
        )
        assert first.controller is not None
        running_id = next(iter(first.controller.state.jobs))
        assert first.controller.state.jobs[running_id].state == "running"

        second = tui_app(
            real_client,
            config=TransportConfig(
                session_id=SESSION_ID,
                thread_id=THREAD_ID,
                mode="resume",
                reconnect_delays=(0.05, 0.1, 0.25),
            ),
            render_interval=0.01,
        )
        async with second.run_test(size=(100, 28)) as second_pilot:
            await wait_for(
                second_pilot,
                lambda: (
                    second.controller is not None
                    and running_id in second.controller.state.jobs
                ),
                description="the second TUI's authoritative jobs snapshot",
            )
            assert second.controller is not None
            assert second.controller.state.jobs[running_id].state == "running"
            assert "1 task running" in status_text(second)
            panel = second.query_one("#jobs", JobPanel)
            assert "subagent" in panel.row_text(running_id)
            assert "running" in panel.row_text(running_id)


@pytest.mark.parametrize("base_url", ["subagent"], indirect=True)
async def test_real_cli_subagent_thread_is_read_only_and_survives_process_resume(
    real_client: XBotClient,
    base_url: str,
    tmp_path: Path,
) -> None:
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for the real TTY interaction smoke")

    await real_client.open_session(
        session_id=SESSION_ID,
        thread_id=THREAD_ID,
        mode="new",
    )
    repo_root = Path(__file__).resolve().parents[3]
    xbot = Path(sys.executable).with_name("xbot")
    first_tmux = f"xbot-subagent-{uuid.uuid4().hex[:10]}"
    resumed_tmux = f"xbot-subagent-resume-{uuid.uuid4().hex[:10]}"
    live_tmux = {first_tmux}
    captures = tmp_path / "subagent-pty-captures"
    captures.mkdir()

    command = shlex.join([
        str(xbot),
        "tui",
        "--server",
        base_url,
        "--session",
        SESSION_ID,
        "--thread",
        THREAD_ID,
        "--workspace",
        str(tmp_path / "workspace"),
    ])

    def launch(name: str) -> None:
        _tmux(
            "new-session",
            "-d",
            "-s",
            name,
            "-x",
            "100",
            "-y",
            "28",
            "-c",
            str(repo_root),
            command,
        )
        _resize_tmux_window(name, 100, 28)

    async def open_child(name: str, child_id: str, child_index: int) -> str:
        _tmux("send-keys", "-t", name, "C-t")
        await _wait_for_tmux_screen(
            name,
            lambda screen: "Agent threads" in screen and child_id in screen,
            description="the real agent thread picker",
        )
        for _ in range(child_index):
            _tmux("send-keys", "-t", name, "Down")
        _tmux("send-keys", "-t", name, "Enter")
        return await _wait_for_tmux_screen(
            name,
            lambda screen: (
                "The reviewer found no blocking issue." in screen
                and f"subagent:{child_id}" in screen
                and "read-only" in screen
            ),
            description="the read-only child transcript",
        )

    try:
        launch(first_tmux)
        await _wait_for_tmux_screen(
            first_tmux,
            lambda screen: "Ready" in screen and f"session:{SESSION_ID}" in screen,
            description="the first real TUI attach",
        )
        _tmux(
            "send-keys",
            "-t",
            first_tmux,
            "-l",
            "Delegate this review and wait for the result",
        )
        _tmux("send-keys", "-t", first_tmux, "Enter")
        parent = await _wait_for_tmux_screen(
            first_tmux,
            lambda screen: "The delegated review is complete." in screen,
            description="the parent completion after delegation",
        )
        assert "<runtime_event" not in parent
        assert '"kind": "job_completed"' not in parent
        assert "ctx:~" in parent and "!" in parent
        (captures / "parent-complete.txt").write_text(parent, encoding="utf-8")

        threads = (await real_client.list_threads(SESSION_ID)).threads
        child = next(item for item in threads if item.kind == "subagent")
        child_index = next(
            index
            for index, item in enumerate(threads)
            if item.thread_id == child.thread_id
        )
        child_screen = await open_child(first_tmux, child.thread_id, child_index)
        (captures / "child-read-only.txt").write_text(
            child_screen, encoding="utf-8"
        )

        _tmux("send-keys", "-t", first_tmux, "Escape")
        await _wait_for_tmux_screen(
            first_tmux,
            lambda screen: (
                "The delegated review is complete." in screen
                and f"subagent:{child.thread_id}" not in screen
            ),
            description="returning to the parent transcript",
        )
        await _stop_tmux_tui(first_tmux)
        live_tmux.discard(first_tmux)
        await real_client.close_session(SESSION_ID)

        launch(resumed_tmux)
        live_tmux.add(resumed_tmux)
        resumed_parent = await _wait_for_tmux_screen(
            resumed_tmux,
            lambda screen: (
                "The delegated review is complete." in screen
                and "session:Subagent review" in screen
                and "Ready" in screen
            ),
            description="the parent transcript in a new TUI process",
        )
        assert "<runtime_event" not in resumed_parent
        assert '"kind": "job_completed"' not in resumed_parent
        assert "ctx:~" in resumed_parent and "!" in resumed_parent
        (captures / "parent-resumed.txt").write_text(
            resumed_parent, encoding="utf-8"
        )
        resumed_child = await open_child(
            resumed_tmux, child.thread_id, child_index
        )
        (captures / "child-resumed.txt").write_text(
            resumed_child, encoding="utf-8"
        )
        await _stop_tmux_tui(resumed_tmux)
        live_tmux.discard(resumed_tmux)
    finally:
        for name in live_tmux:
            try:
                _tmux("kill-session", "-t", name)
            except AssertionError:
                pass


async def test_a_fresh_attach_with_no_session_id_comes_up_ready(
    real_client: XBotClient,
) -> None:
    """The ``xbot tui`` default: no ``--session``, so the server picks the id.

    This is the path a user actually starts with, and it used to end on
    ``Disconnected`` after two 404s: the client kept the empty id it had, read
    ``list_threads("")`` and rejected every frame of its own new session.
    """
    app = tui_app(
        real_client,
        config=TransportConfig(session_id="", thread_id=THREAD_ID, mode="new"),
        render_interval=0.01,
    )
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        assert "Disconnected" not in status_text(app)
        assert app.controller is not None
        assert app.controller.state.session_id, "the assigned session id is adopted"
        composer = app.query_one("#composer", Composer)
        composer.load_text("first question")
        await pilot.press("enter")
        await wait_for(pilot, lambda: REPLY in transcript_text(app), description="reply")


# --- server commands, discovered from the real endpoint -------------------


async def test_the_real_catalogue_is_discovered_not_hardcoded(
    real_client: XBotClient,
) -> None:
    """The client-owned status entry does not replace the server catalogue."""
    app = tui(real_client)
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        assert app.controller is not None
        server_catalog = await app.controller.commands()
        assert any(spec.name == "status" for spec in server_catalog)
        assert "status" in app.commands.names(), "the client exposes its local Status page"
        assert "model" in app.commands.names()
        composer = app.query_one("#composer", Composer)
        composer.load_text("/help")
        await pilot.press("enter")
        await wait_for(pilot, lambda: "/status" in transcript_text(app), description="help")
        composer.load_text("/help undo")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: "Usage: /undo [count]" in transcript_text(app),
            description="server command help",
        )
        rendered = transcript_text(app)
        assert "Remove recent conversation turns" in rendered
        assert "Unknown command" not in rendered


async def test_a_real_server_command_runs_and_answers(real_client: XBotClient) -> None:
    from XBotv2.tui.timeline import UserEntry

    app = tui(real_client)
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        assert app.controller is not None
        composer = app.query_one("#composer", Composer)
        composer.load_text("/status verbose")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: "Usage: /status" in transcript_text(app),
            description="the server's answer on screen",
        )
        assert not [
            entry for entry in app.controller.state.timeline if isinstance(entry, UserEntry)
        ], "a server command is not a chat message"


# --- the client-side report and the pickers, over real HTTP ---------------


async def test_status_is_rendered_from_local_state(real_client: XBotClient) -> None:
    """No ``/status`` round trip: the client has the thread read already."""
    app = tui(real_client)
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        assert app.controller is not None
        session_id = app.controller.state.session_id
        composer = app.query_one("#composer", Composer)
        composer.load_text("/status")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: bool(app.screen.query("#settings-status-report")),
            description="report",
        )
        rendered = str(app.screen.query_one("#settings-status-report", Static).content)
        assert f"ID: {session_id}" in rendered
        assert f"Thread: {THREAD_ID}" in rendered
        assert "State:" in rendered


async def test_the_provider_picker_reads_the_real_catalogue(real_client: XBotClient) -> None:
    from XBotv2.tui.view.selection import SelectionScreen

    app = tui(real_client)
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        composer = app.query_one("#composer", Composer)
        composer.load_text("/provider")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: isinstance(app.screen, SelectionScreen),
            description="the provider picker",
        )
        assert app.controller is not None
        current = app.controller.state.provider
        assert current, "the thread read names the provider"
        await pilot.press(*current)
        await pilot.pause()
        assert current in await screen_text(app), "the picker filters to it"
        await pilot.press("enter")
        for _ in range(60):
            if f"Provider: {current}" in transcript_text(app):
                break
            await asyncio.sleep(0.05)
            await pilot.pause()
        assert f"Provider: {current}" in transcript_text(app), transcript_text(app)


async def test_the_model_picker_reads_the_real_catalogue(real_client: XBotClient) -> None:
    from XBotv2.tui.view.selection import SelectionScreen

    app = tui(real_client)
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        composer = app.query_one("#composer", Composer)
        composer.load_text("/model")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: isinstance(app.screen, SelectionScreen),
            description="the model picker",
        )
        assert app.controller is not None
        current = app.controller.state.model
        assert current, "the thread read names the model"
        await pilot.press(*current)
        await pilot.pause()
        assert current in await screen_text(app), "the picker filters to it"
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: f"Model: {current}" in transcript_text(app),
            description="the selection applied",
        )


# --- the window: bounded attach and paging against the real server --------

@pytest.mark.asyncio
async def test_a_real_window_pages_back_and_names_the_same_nodes(
    real_client: XBotClient,
) -> None:
    """The whole point, against a real server and a real screen.

    The client attaches with a one-record window, is told there is more, loads
    the page before it, and gets the records it did not hold -- each under the
    same identity the server reports for that message.
    """
    app = tui_app(
        real_client,
        config=TransportConfig(
            session_id=SESSION_ID,
            thread_id=THREAD_ID,
            mode="new",
            history_window=1,
        ),
        render_interval=0.01,
    )
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        composer = app.query_one("#composer", Composer)
        composer.load_text("first question")
        await pilot.press("enter")
        await wait_for(pilot, lambda: REPLY in transcript_text(app), description="answer")
        await wait_for(
            pilot,
            lambda: "Running" not in status_text(app),
            description="the turn to finish",
        )
        controller = app.controller
        assert controller is not None

        # Re-attach to the same thread: the live window is replaced by the
        # server's bounded one, which is the point of asking for a window.
        await controller.switch_session(SESSION_ID, THREAD_ID)
        await pilot.pause()

        assert len(controller.state.timeline) == 1, controller.state.timeline.ids()
        assert controller.state.older.__class__.__name__ == "HistoryAvailable"

        await controller.page_older()
        await pilot.pause()

        assert len(controller.state.timeline) == 2, controller.state.timeline.ids()
        assert controller.state.older.__class__.__name__ == "HistoryComplete"
        held = list(controller.state.timeline)
        assert held[0].content == "first question"
        assert held[1].content == REPLY
        assert all(not entry.id.startswith("local:") for entry in held), (
            "a record read from the server is named by the server"
        )

        # The identity the view holds is the server's own identity for the same
        # message, which is what a later page would be merged against.
        page = await real_client.list_messages(SESSION_ID, THREAD_ID, limit=2)
        assert [item.id for item in page.items] == [entry.id for entry in held]


# --- a rewrite, then a page back: ids must not fork -----------------------


@pytest.mark.asyncio
async def test_a_real_regenerate_leaves_one_prompt_and_one_answer(
    real_client: XBotClient,
) -> None:
    """The one operation where a live id and a record id could meet.

    Regenerating removes the last human turn and re-accepts it. The client is
    told about the rewrite *and* about the accepted input, so this is where a
    second copy of the prompt would show up if the two named the same message
    differently -- and where paging back afterwards would expose it.
    """
    app = tui_app(
        real_client,
        config=TransportConfig(
            session_id=SESSION_ID,
            thread_id=THREAD_ID,
            mode="new",
            history_window=2,
        ),
        render_interval=0.01,
    )
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        composer = app.query_one("#composer", Composer)
        composer.load_text("first question")
        await pilot.press("enter")
        await wait_for(pilot, lambda: REPLY in transcript_text(app), description="answer")
        await wait_for(
            pilot,
            lambda: "Running" not in status_text(app),
            description="the turn to finish",
        )

        await real_client.regenerate_message(
            SESSION_ID, THREAD_ID, request_id="regenerate-1"
        )
        await wait_for(
            pilot,
            lambda: "Running" not in status_text(app)
            and transcript_text(app).count("first question") == 1
            and transcript_text(app).count(REPLY) == 1,
            description="one prompt and one answer after regenerating",
        )

        controller = app.controller
        assert controller is not None
        prompts = [
            entry
            for entry in controller.state.timeline
            if getattr(entry, "content", "") == "first question"
        ]
        assert len(prompts) == 1, [entry.id for entry in controller.state.timeline]

        # Paging back must not re-deliver a record the client already holds
        # under a different name.
        while True:
            before = len(controller.state.timeline)
            await controller.page_older()
            await pilot.pause()
            if len(controller.state.timeline) == before:
                break
        ids = [entry.id for entry in controller.state.timeline]
        assert len(ids) == len(set(ids)), ids
        assert sum(
            1
            for entry in controller.state.timeline
            if getattr(entry, "content", "") == "first question"
        ) == 1, ids
