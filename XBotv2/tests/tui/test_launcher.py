"""The Textual adapter translates generic launch facts into an app run."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

import XBotv2.tui.terminal as terminal_module
from XBotv2.application.client import ClientLaunch
from XBotv2.commands.plugin import CommandsService
from XBotv2.tui.config import TextualTuiConfig
from XBotv2.tui.transport import DEFAULT_HISTORY_RETENTION, DEFAULT_HISTORY_WINDOW


class FakeBackend:
    async def close(self) -> None:
        pytest.fail("the client transport plugin owns backend disposal")


def commands_port():
    return CommandsService(ownership="caller")


def launch() -> ClientLaunch:
    return ClientLaunch(
        data_dir="/tmp/xbot-state",
        base_url="http://127.0.0.1:4096",
        uds_path="/tmp/xbot.sock",
        workspace="/workspace",
        session_id="resume-me",
        thread_id="main",
        agent="Reviewer",
    )


class FakeApp:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.exit_reason = None
        self.run_error = None
        self.factory_after_run = None

    async def run_async(self) -> None:
        loop = asyncio.get_running_loop()
        loop.set_task_factory(lambda _loop, coro, **kwargs: asyncio.Task(coro, **kwargs))
        if self.run_error:
            raise self.run_error
        self.factory_after_run = loop.get_task_factory()

    def exit(self, *, message: str) -> None:
        self.exit_reason = message


@pytest.fixture
def fake_app(monkeypatch):
    apps = []

    def construct(**kwargs):
        app = FakeApp(**kwargs)
        apps.append(app)
        return app

    monkeypatch.setattr(terminal_module, "TuiApp", construct)
    return apps


async def test_adapter_builds_app_from_launch_and_plugin_config(fake_app) -> None:
    backend = FakeBackend()
    adapter = terminal_module.TextualTerminalClient(
        backend=backend,
        commands=commands_port(),
        launch=launch(),
        config=TextualTuiConfig(render_interval=0.25, transcript_limit=80),
    )

    await adapter.run()

    assert len(fake_app) == 1
    app = fake_app[0]
    assert app.kwargs["backend"] is backend
    assert app.kwargs["workspace"] == "/workspace"
    assert app.kwargs["render_interval"] == 0.25
    assert app.kwargs["transcript_limit"] == 80
    transport = app.kwargs["config"]
    assert transport.session_id == "resume-me"
    assert transport.thread_id == "main"
    assert transport.agent == "Reviewer"
    assert transport.workspace_root == "/workspace"
    assert transport.mode == "resume"
    assert transport.history_window == DEFAULT_HISTORY_WINDOW
    assert transport.history_retention == DEFAULT_HISTORY_RETENTION


async def test_adapter_uses_new_session_defaults_and_launch_overrides(fake_app) -> None:
    backend = FakeBackend()
    adapter = terminal_module.TextualTerminalClient(
        backend=backend,
        commands=commands_port(),
        launch=replace(
            launch(),
            session_id=None,
            history_window=17,
            history_retention=300,
        ),
        config=TextualTuiConfig(),
    )

    await adapter.run()

    transport = fake_app[0].kwargs["config"]
    assert transport.session_id == ""
    assert transport.mode == "new"
    assert transport.history_window == 17
    assert transport.history_retention == 300


@pytest.mark.parametrize("fails", [False, True])
async def test_adapter_restores_event_loop_factory_after_textual_run(
    fake_app, fails: bool
) -> None:
    adapter = terminal_module.TextualTerminalClient(
        backend=FakeBackend(),
        commands=commands_port(),
        launch=launch(),
        config=TextualTuiConfig(),
    )
    marker = lambda _loop, coro, **kwargs: asyncio.Task(coro, **kwargs)
    loop = asyncio.get_running_loop()
    loop.set_task_factory(marker)
    if fails:
        fake_app[0].run_error = RuntimeError("render failed")

    try:
        if fails:
            with pytest.raises(RuntimeError, match="render failed"):
                await adapter.run()
        else:
            await adapter.run()
        assert loop.get_task_factory() is marker
    finally:
        loop.set_task_factory(None)


async def test_stop_request_is_forwarded_to_textual_app(fake_app) -> None:
    adapter = terminal_module.TextualTerminalClient(
        backend=FakeBackend(),
        commands=commands_port(),
        launch=launch(),
        config=TextualTuiConfig(),
    )

    adapter.request_stop("host shutdown")

    assert fake_app[0].exit_reason == "host shutdown"
