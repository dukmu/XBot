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
from functools import partial
from pathlib import Path
from typing import AsyncIterator, Callable

import pytest
import pytest_asyncio
import uvicorn
import yaml

from XBotv2.client import XBotClient
from XBotv2.core.paths import RuntimePaths
from XBotv2.llm.mock import MockLLM
from XBotv2.application.app import create_agent_application
from XBotv2.application.server import start_server_application
from XBotv2.tests.tui.factories import PNG_BYTES
from XBotv2.tui.app import TuiApp
from XBotv2.tui.transport import TransportConfig
from XBotv2.tui.view.composer import Composer
from XBotv2.tui.view.status_bar import StatusBar
from XBotv2.tui.view.transcript import TranscriptScroll

SESSION_ID = "tui-e2e"
THREAD_ID = "agent"
REPLY = "hello from the real server"


@pytest_asyncio.fixture
async def base_url(tmp_path: Path) -> AsyncIterator[str]:
    """A real XBotv2 HTTP server on an ephemeral port, with a mocked model.

    ``httpx.ASGITransport`` buffers a whole response before returning it, so it
    cannot carry a Server-Sent Events stream at all: the subscription never
    completes. Driving a real uvicorn server is therefore the only way to test the
    client's event reader and everything built on it.
    """
    data_dir = tmp_path / "data"
    (data_dir / "config").mkdir(parents=True)
    (data_dir / "config" / "plugins.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "id": "llm",
                    "name": "llm",
                    "config": {
                        "default": "default",
                        "providers": {
                            "default": {
                                "protocol": "openai",
                                "base_url": "http://test",
                                "api_key": "test",
                                "default_model": "test",
                                "models": [{"model": "test", "max_context_tokens": 4096}],
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
                    "config": {"ask": [{"tool": "ask_user"}, {"tool": "edit"}]},
                },
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = await start_server_application(
        provider_name="default",
        paths=RuntimePaths.from_data_dir(data_dir),
        workspace_root=str(workspace),
        no_plugins=True,
    )
    server.sessions.application_factory = partial(
        create_agent_application,
        model_override=MockLLM(
            responses=[{"content": REPLY}] * 6,
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
            "other-e2e", THREAD_ID, after=opened.event_cursor
        ):
            if frame.type in {"turn_finished", "turn_cancelled"}:
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
        user = [item for item in stored.messages if item.role == "user"]
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
        assert [item.content for item in stored.messages if item.role == "user"] == [
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
