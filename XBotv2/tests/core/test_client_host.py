"""The CLI owns the foreground terminal and its root on one event loop."""
import asyncio
import threading
import pytest
from xcore.api.context import Context
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.tui.api.service import TERMINAL_CLIENT
import XBotv2.main as cli


@pytest.mark.parametrize("outcome", ["ok", "failure", "cancelled"])
async def test_terminal_lifetime_is_awaited_and_root_is_closed(tmp_path, monkeypatch, outcome):
    closed = []
    identity = (asyncio.get_running_loop(), threading.get_ident())
    root = Context()
    class Terminal:
        async def run(self):
            assert (asyncio.get_running_loop(), threading.get_ident()) == identity
            if outcome == "failure":
                raise RuntimeError("terminal failed")
            if outcome == "cancelled":
                raise asyncio.CancelledError()
    root.provide(TERMINAL_CLIENT, Terminal())
    root.dispose(lambda: closed.append((asyncio.get_running_loop(), threading.get_ident())))
    async def open_root(**kwargs):
        await root.start()
        return root
    monkeypatch.setattr(cli, "open_runtime", open_root)
    if outcome == "ok":
        await cli.run_terminal(paths=RuntimePaths.from_data_dir(tmp_path), overrides=None)
    else:
        error = RuntimeError if outcome == "failure" else asyncio.CancelledError
        with pytest.raises(error):
            await cli.run_terminal(paths=RuntimePaths.from_data_dir(tmp_path), overrides=None)
    assert closed == [identity]
    assert not root.is_active
