"""The generic client host owns one async plugin lifetime."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

import XBotv2.application.client as client_host
from XBotv2.core.paths import RuntimePaths
from XBotv2.loader import PluginOverlay


class FakeContext:
    def __init__(self, *, data_dir: str) -> None:
        self.data_dir = data_dir
        self.values = {}
        self.destroy_count = 0
        self.destroy_identity = None

    def set(self, key, value) -> None:
        self.values[key] = value

    def require(self, key):
        return self.values[key]

    async def destroy(self) -> None:
        self.destroy_count += 1
        self.destroy_identity = (asyncio.get_running_loop(), threading.get_ident())


def launch(tmp_path: Path):
    return dict(
        paths=RuntimePaths.from_data_dir(tmp_path / "state"),
        overrides=PluginOverlay.parse([{
            "id": "textual-tui", "config": {"session_id": "resume-me"},
        }]),
    )


async def test_host_runs_terminal_and_disposes_context_on_the_same_loop(
    monkeypatch, tmp_path
) -> None:
    context = FakeContext(data_dir="")
    order = []
    identity = (asyncio.get_running_loop(), threading.get_ident())

    class Terminal:
        async def run(self) -> None:
            order.append("run")
            assert (asyncio.get_running_loop(), threading.get_ident()) == identity

        def request_stop(self, reason: str) -> None:
            order.append(("stop", reason))

    terminal = Terminal()
    monkeypatch.setattr(client_host, "Context", lambda *, data_dir: context)
    def load_tree(*, paths, overrides):
        assert paths.data_dir == tmp_path / "state"
        assert overrides.patches == launch(tmp_path)["overrides"].patches
        return "client-tree"

    monkeypatch.setattr(client_host, "load_client_tree", load_tree)

    async def boot_application(*, ctx, tree):
        assert ctx is context
        assert tree == "client-tree"
        assert ctx.values == {}
        ctx.set("terminal_client", terminal)
        return ctx

    monkeypatch.setattr(client_host, "boot_application", boot_application)
    context.destroy = lambda: _destroy(context, order, identity)

    await client_host.run_client_application(**launch(tmp_path))

    assert order == ["run", "destroy"]
    assert context.destroy_count == 1
    assert context.destroy_identity == identity


async def test_host_disposes_context_when_terminal_raises(monkeypatch, tmp_path) -> None:
    context = FakeContext(data_dir="")

    class Terminal:
        async def run(self) -> None:
            raise RuntimeError("terminal failed")

        def request_stop(self, reason: str) -> None:
            pass

    monkeypatch.setattr(client_host, "Context", lambda *, data_dir: context)
    monkeypatch.setattr(client_host, "load_client_tree", lambda **kwargs: "tree")

    async def boot_application(*, ctx, tree):
        ctx.set("terminal_client", Terminal())
        return ctx

    monkeypatch.setattr(client_host, "boot_application", boot_application)

    with pytest.raises(RuntimeError, match="terminal failed"):
        await client_host.run_client_application(**launch(tmp_path))

    assert context.destroy_count == 1


async def test_host_does_not_repeat_boot_failure_cleanup(monkeypatch, tmp_path) -> None:
    context = FakeContext(data_dir="")

    monkeypatch.setattr(client_host, "Context", lambda *, data_dir: context)
    monkeypatch.setattr(client_host, "load_client_tree", lambda **kwargs: "tree")

    async def boot_application(*, ctx, tree):
        await ctx.destroy()
        raise RuntimeError("plugin start failed")

    monkeypatch.setattr(client_host, "boot_application", boot_application)

    with pytest.raises(RuntimeError, match="plugin start failed"):
        await client_host.run_client_application(**launch(tmp_path))

    assert context.destroy_count == 1


async def test_host_disposes_context_when_terminal_is_cancelled(
    monkeypatch, tmp_path
) -> None:
    context = FakeContext(data_dir="")
    entered = asyncio.Event()

    class Terminal:
        async def run(self) -> None:
            entered.set()
            await asyncio.Future()

        def request_stop(self, reason: str) -> None:
            pass

    monkeypatch.setattr(client_host, "Context", lambda *, data_dir: context)
    monkeypatch.setattr(client_host, "load_client_tree", lambda **kwargs: "tree")

    async def boot_application(*, ctx, tree):
        ctx.set("terminal_client", Terminal())
        return ctx

    monkeypatch.setattr(client_host, "boot_application", boot_application)
    task = asyncio.create_task(client_host.run_client_application(**launch(tmp_path)))
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert context.destroy_count == 1


async def _destroy(context: FakeContext, order: list, identity) -> None:
    order.append("destroy")
    await FakeContext.destroy(context)
    assert (asyncio.get_running_loop(), threading.get_ident()) == identity
