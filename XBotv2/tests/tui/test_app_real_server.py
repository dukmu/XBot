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
from textual.widgets import Static

from XBotv2.client import XBotClient
from XBotv2.core.domain import ProviderError
from XBotv2.core.history import SurfaceReplaced
from XBotv2.core.messages import CompactionSummaryMessage
from XBotv2.core.stream import ModelFailed
from XBotv2.core.paths import RuntimePaths
from XBotv2.interactions.protocol import UserInputRequest
from XBotv2.llm.mock import MockLLM
from XBotv2.permissions.contracts import PermissionRequest
from XBotv2.application.app import create_agent_application
from XBotv2.application.server import start_server_application
from XBotv2.session.records import AssistantRecord, HumanInputRecord
from XBotv2.tests.tui.factories import PNG_BYTES
from XBotv2.tui.app import TuiApp
from XBotv2.tui.transport import TransportConfig
from XBotv2.tui.view.composer import Composer
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
        "core", "permission", "permission_deny", "interaction"
    }
    (data_dir / "config").mkdir(parents=True)
    plugin_entries = [
        {
            "id": "llm",
            "name": "llm",
            "config": {
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
            },
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
            if not (scenario == "compact" and plugin_id == "compact")
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
    server = await start_server_application(
        provider_name="default",
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
    elif scenario == "compact":
        model_responses = [
            {"content": CAPTION},
            *[{'content': f'answer {index}'} for index in range(5)],
            {"content": "summary of the earlier discussion"},
        ]
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
    return TuiApp(
        backend=client,
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
            lambda screen: "ask_user" in screen and "ID:" in screen,
            description="the permission request rendered in the real TTY",
        )
        (captures / "permission.txt").write_text(
            permission_screen, encoding="utf-8"
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
        _tmux(
            "send-keys",
            "-t",
            session_name,
            "-l",
            f"/approve {permission.interaction_id} once",
        )
        _tmux("send-keys", "-t", session_name, "Enter")
        question_screen = await _wait_for_tmux_screen(
            session_name,
            lambda screen: "Which deployment window?" in screen,
            description="the user-input request after permission approval",
        )
        (captures / "question.txt").write_text(question_screen, encoding="utf-8")

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
        _tmux(
            "send-keys",
            "-t",
            session_name,
            "-l",
            f"/answer {question.interaction_id} Tuesday",
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
async def test_caption_updates_the_real_tui_session_bar(real_client: XBotClient) -> None:
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
        session_bar = app.query_one("#session", Static).content
        session_text = str(getattr(session_bar, "plain", "") or "")
        assert CAPTION in session_text

        # Switching away and back must adopt the persisted thread title into
        # both state and the fixed session bar, not just retain a stale label.
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
        session_bar = app.query_one("#session", Static).content
        session_text = str(getattr(session_bar, "plain", "") or "")
        assert CAPTION in session_text

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
        session_content = app.query_one("#session", Static).content
        session_text = str(getattr(session_content, "plain", "") or "")
        assert CAPTION in session_text

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

        composer.load_text(f"/approve {request.interaction_id} once")
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

        composer.load_text(f"/deny {request.interaction_id}")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: REPLY in transcript_text(app),
            description="the model reply after permission denial",
        )
        assert not app.controller.state.pending_interactions
        assert "→ denied" in transcript_text(app)
        assert not (tmp_path / "workspace" / "denied.txt").exists()


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

        composer.load_text(f"/approve {permission.interaction_id} once")
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
        composer.load_text(f"/answer {question.interaction_id} Thursday")
        await pilot.press("enter")
        await wait_for(
            pilot,
            lambda: REPLY in transcript_text(app),
            description="the model reply after receiving the answer",
        )
        assert not app.controller.state.pending_interactions


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
        assert "in:120" not in status_text(app)
        assert "out:35" not in status_text(app)
        assert "cache:20%" not in status_text(app)
        assert "ctx:~" not in status_text(app)
        session_bar = app.query_one("#session", Static).content
        session_text = str(getattr(session_bar, "plain", "") or "")
        assert "session:tui-e2e" in session_text
        assert "in:120" in session_text
        assert "cache:20%" in session_text
        assert "ctx:~" in session_text
        assert "/4096" in session_text


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


async def test_a_fresh_attach_with_no_session_id_comes_up_ready(
    real_client: XBotClient,
) -> None:
    """The ``xbot tui`` default: no ``--session``, so the server picks the id.

    This is the path a user actually starts with, and it used to end on
    ``Disconnected`` after two 404s: the client kept the empty id it had, read
    ``list_threads("")`` and rejected every frame of its own new session.
    """
    app = TuiApp(
        backend=real_client,
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
    """``/status`` is not a client command: it exists because the server says so."""
    app = tui(real_client)
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(pilot, lambda: "Ready" in status_text(app), description="Ready")
        offered = [spec.name for name in app.commands.names() if (spec := app.commands.get(name))]
        assert "status" in offered, f"the server's catalogue was not adopted: {offered}"
        assert "model" in offered
        composer = app.query_one("#composer", Composer)
        composer.load_text("/help")
        await pilot.press("enter")
        await wait_for(pilot, lambda: "/status" in transcript_text(app), description="help")


async def test_a_real_server_command_runs_and_answers(real_client: XBotClient) -> None:
    from XBotv2.tui.timeline import UserEntry

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
            lambda: session_id in transcript_text(app),
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
            pilot, lambda: f"ID: {session_id}" in transcript_text(app), description="report"
        )
        rendered = transcript_text(app)
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
    app = TuiApp(
        backend=real_client,
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
    app = TuiApp(
        backend=real_client,
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
