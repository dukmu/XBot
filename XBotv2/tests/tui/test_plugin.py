"""The selected client profile composes Textual from public services."""

from __future__ import annotations

import asyncio
import threading
import importlib

from xcore import Context

from XBotv2.application.boot import boot_application
from XBotv2.loader import PluginOverlay
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
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "plugins.yaml").write_text(
        "- id: client-transport\n"
        "  config:\n"
        "    base_url: http://configured.example:4096\n"
        "- id: textual-tui\n"
        "  config:\n"
        "    history_window: 17\n"
        "    session_id: configured-session\n",
        encoding="utf-8",
    )
    context = Context(data_dir=paths.data_dir)
    context = await boot_application(
        ctx=context,
        tree=load_client_tree(paths=paths, overrides=PluginOverlay.parse([
            {"id": "client-transport", "config": {"base_url": "http://127.0.0.1:4096"}},
            {"id": "textual-tui", "config": {"workspace": str(tmp_path)}},
        ])),
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
        assert terminal.app.transport_config.history_window == 17
        assert terminal.app.transport_config.session_id == "configured-session"
        assert not context.has("client_launch")
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
