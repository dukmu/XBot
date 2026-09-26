"""The selected client profile composes Textual from public services."""

from __future__ import annotations

import asyncio
import threading
import importlib

from xcore import Context

from XBotv2.application.boot import boot_application
from XBotv2.application.client import ClientLaunch
from XBotv2.application.tree import load_client_tree
from XBotv2.core.paths import RuntimePaths
from XBotv2.tui.app import TuiApp
from XBotv2.tui.terminal import TextualTerminalClient


async def test_client_profile_provides_one_api_client_and_textual_terminal(
    monkeypatch, tmp_path
) -> None:
    identities = []

    class FakeClient:
        closed = False

        def __init__(self, base_url: str, *, uds_path: str | None) -> None:
            self.base_url = base_url
            self.uds_path = uds_path
            self.identity = (asyncio.get_running_loop(), threading.get_ident())

        async def close(self) -> None:
            self.closed = True
            identities.append(("close", asyncio.get_running_loop(), threading.get_ident()))

    async def run_app(self) -> None:
        identities.append(("run", asyncio.get_running_loop(), threading.get_ident()))

    monkeypatch.setattr(TuiApp, "run_async", run_app)

    client_transport_module = importlib.import_module(
        "XBotv2.client_transport.plugin"
    )
    monkeypatch.setattr(client_transport_module, "XBotClient", FakeClient)
    paths = RuntimePaths.from_data_dir(tmp_path / "state")
    context = Context(data_dir=paths.data_dir)
    launch = ClientLaunch(
        data_dir=str(paths.data_dir),
        base_url="http://127.0.0.1:4096",
        uds_path=None,
        workspace=str(tmp_path),
        session_id=None,
        thread_id="agent",
        agent=None,
    )
    context.set("runtime_paths", paths)
    context.set("client_launch", launch)

    context = await boot_application(
        ctx=context,
        tree=load_client_tree(paths=paths),
    )
    try:
        api = context.require("client_api")
        terminal = context.require("terminal_client")
        assert isinstance(api, FakeClient)
        assert isinstance(terminal, TextualTerminalClient)
        commands = context.require("commands")
        assert {command.name for command in commands.all()} >= {
            "help",
            "status",
            "session",
            "thread",
            "provider",
            "model",
            "agent",
            "thinking",
        }
        assert api.base_url == "http://127.0.0.1:4096"
        assert api.uds_path is None
        assert not api.closed
        await terminal.run()
    finally:
        await context.destroy()

    assert api.closed
    assert identities == [
        ("run", api.identity[0], api.identity[1]),
        ("close", api.identity[0], api.identity[1]),
    ]
    assert commands.all() == ()
