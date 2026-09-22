"""The launcher: the one entry point ``xbot tui`` uses.

Written before the seam it needs existed. A launcher is easy to leave untested and
then get wrong in the ways that matter: not closing the client when the UI raises,
or quietly dropping an argument on the way to the app.
"""

from __future__ import annotations

import pytest

from XBotv2.tui.app import TuiApp, run_tui


class FakeBackend:
    """Records being closed; nothing else is reached in these tests."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def run_app(monkeypatch):
    """Run the real app class without driving a terminal."""
    ran: list[TuiApp] = []

    async def fake_run_async(self) -> None:
        ran.append(self)

    monkeypatch.setattr(TuiApp, "run_async", fake_run_async)
    return ran


async def test_the_launcher_runs_the_app(run_app) -> None:
    backend = FakeBackend()
    await run_tui(client_factory=lambda **_kwargs: backend)
    assert len(run_app) == 1
    assert run_app[0].transport_config.session_id == ""


async def test_the_launcher_closes_the_client(run_app) -> None:
    backend = FakeBackend()
    await run_tui(client_factory=lambda **_kwargs: backend)
    assert backend.closed is True


async def test_the_launcher_closes_the_client_even_when_the_ui_raises(
    monkeypatch,
) -> None:
    """A crash in the UI must not leak the HTTP client."""

    async def explode(self) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(TuiApp, "run_async", explode)
    backend = FakeBackend()
    with pytest.raises(RuntimeError, match="boom"):
        await run_tui(client_factory=lambda **_kwargs: backend)
    assert backend.closed is True


async def test_the_launcher_forwards_what_the_cli_decided(run_app) -> None:
    backend = FakeBackend()
    await run_tui(
        base_url="http://127.0.0.1:9999",
        session_id="resumed",
        thread_id="main",
        agent="Reviewer",
        workspace_root="/workspace",
        mode="resume",
        render_interval=0.25,
        client_factory=lambda **_kwargs: backend,
    )
    config = run_app[0].transport_config
    assert config.session_id == "resumed"
    assert config.thread_id == "main"
    assert config.agent == "Reviewer"
    assert config.workspace_root == "/workspace"
    assert config.mode == "resume"
    assert run_app[0].workspace == "/workspace"
    assert run_app[0].render_interval == 0.25


async def test_the_launcher_carries_the_socket_path_to_the_client(run_app) -> None:
    """One client per launch, built from the location the CLI resolved."""
    seen: list[dict] = []

    def factory(**kwargs):
        seen.append(kwargs)
        return FakeBackend()

    await run_tui(base_url="http://127.0.0.1:1", uds_path="/tmp/x.sock", client_factory=factory)
    assert seen and seen[0]["base_url"] == "http://127.0.0.1:1"
    assert seen[0]["uds_path"] == "/tmp/x.sock"
